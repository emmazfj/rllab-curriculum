import os
from rllab.baselines.gaussian_mlp_baseline import GaussianMLPBaseline
from rllab.envs.box2d.cartpole_swingup_env import CartpoleSwingupEnv
from rllab.envs.box2d.double_pendulum_env import DoublePendulumEnv
from rllab.envs.box2d.mountain_car_env import MountainCarEnv
from sandbox.rein.envs.gym_env_downscaled import GymEnv
from sandbox.rein.envs.double_pendulum_env_x import DoublePendulumEnvX
from sandbox.rein.envs.cartpole_swingup_env_x import CartpoleSwingupEnvX
from rllab.policies.categorical_mlp_policy import CategoricalMLPPolicy
from rllab.core.network import ConvNetwork

os.environ["THEANO_FLAGS"] = "device=gpu"

from rllab.envs.box2d.cartpole_env import CartpoleEnv
from rllab.policies.gaussian_mlp_policy import GaussianMLPPolicy
from rllab.envs.normalized_env import NormalizedEnv

from sandbox.rein.algos.trpo import TRPO
# from sandbox.john.instrument import stub, run_experiment_lite
from rllab.misc.instrument import stub, run_experiment_lite

import itertools

RECORD_VIDEO = True
num_seq_frames = 4

stub(globals())

# Param ranges
seeds = range(10)

mdps = [GymEnv("Freeway-v0", record_video=RECORD_VIDEO),
        GymEnv("Breakout-v0", record_video=RECORD_VIDEO),
        GymEnv("Frostbite-v0", record_video=RECORD_VIDEO),
        GymEnv("MontezumaRevenge-v0", record_video=RECORD_VIDEO)]
param_cart_product = itertools.product(
    mdps, seeds
)

for mdp, seed in param_cart_product:
    network = ConvNetwork(
        input_shape=(num_seq_frames,) + (mdp.spec.observation_space.shape[1], mdp.spec.observation_space.shape[2]),
        output_dim=mdp.spec.action_space.flat_dim,
        hidden_sizes=(64,),
        conv_filters=(16, 16),
        conv_filter_sizes=(4, 4),
        conv_strides=(2, 2),
        conv_pads=(0, 0),
    )
    policy = CategoricalMLPPolicy(
        env_spec=mdp.spec,
        prob_network=network,
    )

    network = ConvNetwork(
        input_shape=(num_seq_frames,) + (mdp.spec.observation_space.shape[1], mdp.spec.observation_space.shape[2]),
        output_dim=1,
        hidden_sizes=(64,),
        conv_filters=(16, 16),
        conv_filter_sizes=(4, 4),
        conv_strides=(2, 2),
        conv_pads=(0, 0),
    )
    baseline = GaussianMLPBaseline(
        mdp.spec,
        num_seq_inputs=num_seq_frames,
        regressor_args=dict(
            mean_network=network,
            subsample_factor=0.5),
    )

    algo = TRPO(
        discount=0.995,
        env=mdp,
        policy=policy,
        baseline=baseline,
        batch_size=10000,
        whole_paths=True,
        max_path_length=5000,
        n_itr=250,
        step_size=0.01,
        sampler_args=dict(num_seq_frames=num_seq_frames),
        optimizer_args=dict(
            num_slices=30,
            subsample_factor=0.1),
    )

    run_experiment_lite(
        algo.train(),
        exp_prefix="trpo-atari-d",
        n_parallel=4,
        snapshot_mode="last",
        seed=seed,
        mode="lab_kube",
        use_gpu=False,
        dry=False,
    )