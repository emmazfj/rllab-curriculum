


from rllab.algos.trpo import TRPO
from rllab.baselines.linear_feature_baseline import LinearFeatureBaseline
from rllab.baselines.exact_model_baseline import ExactModelBaseline
from rllab.envs.mujoco.half_cheetah_env import HalfCheetahEnv
from rllab.envs.mujoco.walker2d_env import Walker2DEnv
from rllab.misc.instrument import stub, run_experiment_lite
from rllab.policies.gaussian_mlp_policy import GaussianMLPPolicy

import itertools

stub(globals())

# Name
exp_prefix = "env-sampling-baseline-test-1"

# Settings
local = True
visualize = False
debug = True
n_itr = 200

# Experiment parameters
envs = [HalfCheetahEnv(), Walker2DEnv()]
gae_lambdas = [1]
discounts = [0.99]
step_sizes = [0.1]
seeds = [1, 21, 31, 41, 51]
batch_sizes = [1000]
lookaheads = [0, 1, 2, 5, 10]
max_path_lengths = [100]

# Handling code for experiment configuration
# envs = [normalize(env) for env in envs]
plot = local and visualize
mode = "local" if local else "ec2"
terminate_machine = not debug
if debug:
    exp_prefix += "-DEBUG"

# Experiments
configurations = list(itertools.product(
    envs,
    gae_lambdas,
    discounts,
    step_sizes,
    batch_sizes,
    seeds,
    max_path_lengths))
if debug:
    configurations = [configurations[0]]
    lookaheads = [1, 2]
    n_itr = 5
if not local:
    print("Number of EC2 instances to launch: {}".format(
        len(configurations) * (len(lookaheads) + 1)))
for env, gae_lambda, discount, step_size, batch_size, seed, max_path_length in configurations:

    if not debug:
        policy = GaussianMLPPolicy(
            env_spec=env.spec,
            hidden_sizes=(32, 32)
        )

        linear_baseline_algo = TRPO(
            env=env,
            policy=policy,
            baseline=LinearFeatureBaseline(env_spec=env.spec),
            batch_size=batch_size,
            whole_paths=True,
            max_path_length=max_path_length,
            n_itr=n_itr,
            gae_lambda=gae_lambda,
            discount=discount,
            step_size=step_size,
            plot=plot,
        )

        run_experiment_lite(
            linear_baseline_algo.train(),
            n_parallel=1,
            snapshot_mode="last",
            seed=seed,
            plot=plot,
            mode=mode,
            exp_prefix=exp_prefix,
            terminate_machine=terminate_machine
        )

    for lookahead in lookaheads:

        policy = GaussianMLPPolicy(
            env_spec=env.spec,
            hidden_sizes=(32, 32)
        )

        baseline = ExactModelBaseline(env_spec=env.spec,
                                      lookahead=lookahead,
                                      num_rollouts_per_state=batch_size / max_path_length,
                                      discount=discount,
                                      env=env,
                                      )

        model_baseline_algo = TRPO(
            env=env,
            policy=policy,
            baseline=baseline,
            batch_size=batch_size,
            whole_paths=True,
            max_path_length=max_path_length,
            n_itr=n_itr,
            gae_lambda=gae_lambda,
            discount=discount,
            step_size=step_size,
            plot=plot,
        )

        run_experiment_lite(
            model_baseline_algo.train(),
            n_parallel=1,
            snapshot_mode="last",
            seed=seed,
            plot=plot,
            mode=mode,
            exp_prefix=exp_prefix,
            terminate_machine=terminate_machine
        )