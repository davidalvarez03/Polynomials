# -*- coding: utf-8 -*-
"""JAX_NONLINEAR.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1AzM8QvATIL7be_IHl2daWMaQ1tmSGmnD
"""

import functools
import time

import jax.random
import optax
import tensorflow_probability.substrates.jax as tfp
from jax import jit, pmap, grad, vmap
from jax import numpy as jnp
from tensorflow_probability.substrates.jax import (
    distributions as tfd,
    bijectors as tfb,
    experimental as tfe,
)
from tqdm.auto import trange
import matplotlib.pyplot as plt
import numpy as np
import matplotlib as mpl
import tensorflow_probability.substrates.jax as tfp
import jax
from scipy.optimize import minimize
from jax import random
import optax
from jax import numpy as jnp
tfd = tfp.distributions

from corner import corner

#MAP
def MAP(
        optimizer: optax.GradientTransformation,
        start=None,
        n_samples=500,
        num_steps=350,
        seed=0,
    ):
        dev_cnt = jax.device_count()
        n_samples = (n_samples // dev_cnt) * dev_cnt
        seed = jax.random.PRNGKey(seed)

        start = (
            prior.sample(n_samples, seed=seed)
            if start is None
            else start
        )

        params=jnp.stack((start['p_a'],start['p_b'])).T

        opt_state = optimizer.init(params)

        def loss2(z):
            vectorized_logposterior_1=jax.vmap(logposterior, in_axes=(0))
            vectorized_logposterior_2=jax.vmap(vectorized_logposterior_1, in_axes=(0))
            lp = -1*vectorized_logposterior_2(z)
            return lp

        g=grad(loss)
        g1=jax.vmap(g, in_axes=(0))
        g2=jax.vmap(g1,in_axes=(0))

        def update(params, opt_state):
            splt_params = jnp.array(jnp.split(params, dev_cnt, axis=0))
            splt_params = np.reshape(splt_params,(dev_cnt,n_samples//dev_cnt,2))
            grads = g2(splt_params)
            grads1=np.reshape(grads,(dev_cnt*(n_samples//dev_cnt),2))
            updates, opt_state = optimizer.update(grads1, opt_state)
            new_params = optax.apply_updates(params, updates)
            return new_params, opt_state
        with trange(num_steps) as pbar:
            for _ in pbar:
                params, opt_state = update(params, opt_state)

        return params

def SVI(
        start,
        optimizer: optax.GradientTransformation,
        n_vi=250,
        init_scales=1e-3,
        num_steps=500,
        seed=0,
    ):
        dev_cnt = jax.device_count()
        seeds = jax.random.split(jax.random.PRNGKey(seed), dev_cnt)
        n_vi = (n_vi // dev_cnt) * dev_cnt
        scale = (
            jnp.diag(jnp.ones(jnp.size(start))) * init_scales
            if jnp.size(init_scales) == 1
            else init_scales
        )
        cov_bij = tfp.bijectors.FillScaleTriL(diag_bijector=tfp.bijectors.Exp(), diag_shift=1e-6)
        qz_params = jnp.concatenate(
            [jnp.squeeze(start), cov_bij.inverse(scale)], axis=0
        )
        replicated_params = jax.tree_map(lambda x: jnp.array([x] * dev_cnt), qz_params)

        n_params = jnp.size(start)

        def elbo(qz_params, seed):
            mean = qz_params[:n_params]
            cov = cov_bij.forward(qz_params[n_params:])
            qz = tfd.MultivariateNormalTriL(loc=mean, scale_tril=cov)
            z = qz.sample(n_vi // dev_cnt, seed=seed)
            lps = qz.log_prob(z)
            return jnp.mean(lps - logposterior(qz_params))

        elbo_and_grad = jit(jax.value_and_grad(jit(elbo), argnums=(0,)))

        @functools.partial(pmap, axis_name="num_devices")
        def get_update(qz_params, seed):
            val, grad = elbo_and_grad(qz_params, seed)
            return jax.lax.pmean(val, axis_name="num_devices"), jax.lax.pmean(
                grad, axis_name="num_devices"
            )

        opt_state = optimizer.init(replicated_params)
        loss_hist = []
        with trange(num_steps) as pbar:
            for step in pbar:
                loss, (grads,) = get_update(replicated_params, seeds)
                loss = float(jnp.mean(loss))
                seeds = jax.random.split(seeds[0], dev_cnt)
                updates, opt_state = optimizer.update(grads, opt_state)
                replicated_params = optax.apply_updates(replicated_params, updates)
                pbar.set_description(f"ELBO: {loss:.3f}")
                loss_hist.append(loss)
        mean = replicated_params[0, :n_params]
        cov = cov_bij.forward(replicated_params[0, n_params:])
        qz = tfd.MultivariateNormalTriL(loc=mean, scale_tril=cov)
        return qz, loss_hist

def HMC(

        q_z,
        init_eps=0.3,
        init_l=3,
        n_hmc=50,
        num_burnin_steps=250,
        num_results=750,
        max_leapfrog_steps=30,
        seed=0,
    ):
        dev_cnt = jax.device_count()
        seeds = jax.random.split(jax.random.PRNGKey(seed), dev_cnt)
        n_hmc = (n_hmc // dev_cnt) * dev_cnt
        momentum_distribution = tfd.MultivariateNormalFullCovariance(
            loc=jnp.zeros_like(q_z.mean()),
            covariance_matrix=jnp.linalg.inv(q_z.covariance()),
        )

        '''@jit
        def log_prob(z):
            return self.prob_model.log_prob(lens_sim, z)[0]'''




        @pmap
        def run_chain(seed):
            start = q_z.sample(n_hmc // dev_cnt, seed=seed)
            num_adaptation_steps = int(num_burnin_steps * 0.8)
            qz_params = jnp.squeeze(start) #this step could be removed
            print('shape of start',np.shape(start))


            mc_kernel = tfe.mcmc.PreconditionedHamiltonianMonteCarlo(
                target_log_prob_fn=jax.vmap(logposterior, in_axes=(0)),
                momentum_distribution=momentum_distribution,
                step_size=init_eps,
                num_leapfrog_steps=init_l,
            )

            mc_kernel = tfe.mcmc.GradientBasedTrajectoryLengthAdaptation(
                mc_kernel,
                num_adaptation_steps=num_adaptation_steps,
                max_leapfrog_steps=max_leapfrog_steps,
            )
            mc_kernel = tfp.mcmc.DualAveragingStepSizeAdaptation(
                inner_kernel=mc_kernel, num_adaptation_steps=num_adaptation_steps
            )

            return tfp.mcmc.sample_chain(
                num_results=num_results,
                num_burnin_steps=num_burnin_steps,
                current_state=start,
                trace_fn=lambda _, pkr: None,
                seed=seed,
                kernel=mc_kernel,
            )

        start = time.time()
        ret = run_chain(seeds)
        end = time.time()
        print(f"Sampling took {(end - start):.1f}s")
        return ret

# The true parameters
TRUE_A = 9.0
TRUE_B = 4.0
NUM_DATA = 500
delta=0.1

# A vector of random x values
x = jnp.linspace(2,6, NUM_DATA)

def f(x):
  return TRUE_A/((x-TRUE_B)**2+delta)

mean=0
std=10

# Generate some noise
noise = mean + std*jax.random.normal(key = random.PRNGKey(1),shape=[NUM_DATA],)

# Calculate y
y = f(x) + noise

# Plot all the data
plt.plot(x, y, '.')
plt.show()



TRUE_VALUES=jnp.array([TRUE_A,TRUE_B])
print(TRUE_VALUES)

#DEFINING THE PRIOR, LIKELIHOOD AND POSTERIOR

def logprior(theta):
    p_a=tfd.Normal(7.5,5)
    p_b=tfd.Normal(5,3)
    return p_a.log_prob(theta[0])+p_b.log_prob(theta[1])

def loglikelihood(theta):
    predicted_y=theta[0]/((x-theta[1])**2+delta)
    ll = jax.numpy.sum(jax.scipy.stats.norm.logpdf(predicted_y, loc=y, scale=std))
    return ll

def logposterior(theta):
  return logprior(theta)+loglikelihood(theta)

def loss(theta):
  return -logposterior(theta)

prior = tfd.JointDistributionNamed(dict(p_a=tfd.Normal(7.5,5), p_b=tfd.Normal(5,3),p_c=tfd.Normal(4,3),))

start=prior.sample(4,seed=jax.random.PRNGKey(0))
print(start)

schedule_fn = optax.polynomial_schedule(init_value=-1e-2, end_value=-1e-2/3,
                                      power=0.5, transition_steps=500)

opt = optax.chain(
  optax.scale_by_adam(),
  optax.scale_by_schedule(schedule_fn),
)

map_estimate = MAP(n_samples=10,optimizer=opt, seed=0)

print(map_estimate)

vectorized_logposterior=jax.vmap(logposterior, in_axes=(0))
lps = vectorized_logposterior(map_estimate)
theta_map = map_estimate[jnp.argmax(lps)][jnp.newaxis,:][0]
print(theta_map)
num_params=len(theta_map)

plt.plot(x, y, '.', label="Data")
plt.plot(x, f(x), label="Ground truth")
plt.plot(x, theta_map[0]/((x-theta_map[1])**2+delta), label="MAP Predictions")
plt.legend()
plt.show()
print("Current loss: %1.6f" % loss(theta_map))

schedule_fn = optax.polynomial_schedule(init_value=-1e-6, end_value=-3e-3,
                                      power=2, transition_steps=300)
opt = optax.chain(
  optax.scale_by_adam(),
  optax.scale_by_schedule(schedule_fn),)

q_z, losses = SVI(start=theta_map, optimizer=opt, n_vi=1000, num_steps=1500)

print(q_z.mean())
print(q_z.stddev())
plt.figure()
plt.plot(losses)
plt.savefig('/global/homes/d/davalv03/nonlinear_plots/losses.png')

theta_svi=q_z.mean()
print(theta_svi)
plt.figure()
plt.plot(x, y, '.', label="Data")
plt.plot(x, f(x), label="Ground truth")
plt.plot(x, theta_svi[0]/((x-theta_svi[1])**2+delta), label="SVI Predictions")
plt.legend()
plt.show()
plt.savefig('/global/homes/d/davalv03/nonlinear_plots/plot_SVI.png')
print("Current loss: %1.6f" % loss(theta_svi))

n_hmc=25
num_results=750
dev_cnt=jax.device_count()
num_params=2
samples,sample_stats= HMC(q_z,init_eps=0.3,
        init_l=3,
        n_hmc=n_hmc,
        num_burnin_steps=250,
        num_results=num_results,
        max_leapfrog_steps=30,
        seed=0,
    )

samples_res=np.reshape(samples,(num_results,dev_cnt*(n_hmc//dev_cnt),num_params))
print(np.shape(samples_res))

Rhat = tfp.mcmc.potential_scale_reduction(jnp.array(samples_res))
print(Rhat)

shape_samples=num_results*dev_cnt*(n_hmc//dev_cnt)
samples_res=np.array(samples_res)
s_R2=samples_res.reshape((shape_samples,num_params))

figure=corner(s_R2, show_titles=True,truths=TRUE_VALUES,labels=['A', 'B'])
plt.show()
plt.savefig('/global/homes/d/davalv03/nonlinear_plots/corner_plot.png')
plt.figure()

from scipy.stats import norm
colors = ['#1f0a1d','limegreen','seagreen','navy','darkkhaki']
fig = plt.figure(figsize=(10, 3))
gs = plt.GridSpec(1, 2, width_ratios=[1, .4], height_ratios=[1])
ax1 = plt.subplot(gs[0, 0],)

for i in range(4):
 ax1.plot(samples_res[:,i,0], colors[i], label = f'{i}', alpha = 0.6)
 plt.title('A trace')

ax1.grid(True)

ax2 = plt.subplot(gs[0, 1], sharey=ax1)
for i in range(4):
  prob, bins = np.histogram(samples_res[:,i,0], density = True, bins = 40)
  (mu, sigma) = norm.fit(samples_res[:,i,0])

  y = norm.pdf(bins, mu, sigma)
  ax2.plot(y, bins, colors[i], linewidth=2, alpha = 0.8)

ax2.grid(True)
plt.gca().axes.get_xaxis().set_visible(False)
plt.tick_params(axis='y', labelsize=0)
plt.gca().axes.get_yaxis().set_visible(True)
plt.tight_layout()
plt.show()
plt.savefig('/global/homes/d/davalv03/nonlinear_plots/trace_A.png')
plt.figure()

colors = ['mediumaquamarine',
'teal',
'orchid',
'midnightblue',
'slategray']

fig = plt.figure(figsize=(10, 3))
gs = plt.GridSpec(1, 2, width_ratios=[1, .4], height_ratios=[1])
ax1 = plt.subplot(gs[0, 0],)

for i in range(4):
 ax1.plot(samples_res[:,i,1], colors[i], label = f'{i}', alpha = 0.6)
 plt.title('B trace')


ax1.grid(True)

ax2 = plt.subplot(gs[0, 1], sharey=ax1)
for i in range(4):
  prob, bins = np.histogram(samples_res[:,i,1], density = True, bins = 40)
  (mu, sigma) = norm.fit(samples_res[:,i,1])

  y = norm.pdf(bins, mu, sigma)
  ax2.plot(y, bins, colors[i], linewidth=2, alpha = 0.8)

ax2.grid(True)
plt.gca().axes.get_xaxis().set_visible(False)
plt.tick_params(axis='y', labelsize=0)
plt.gca().axes.get_yaxis().set_visible(True)
plt.tight_layout()
plt.show()
plt.savefig('/global/homes/d/davalv03/nonlinear_plots/trace_B.png')
plt.figure()
