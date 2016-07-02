from __future__ import print_function
from __future__ import absolute_import

from rllab.algos.npo import NPO
from rllab.baselines.linear_feature_baseline import LinearFeatureBaseline
# from rllab.envs.box2d.cartpole_env import CartpoleEnv
from rllab.envs.box2d.cartpole_swingup_env import CartpoleSwingupEnv
from rllab.envs.normalized_env import normalize
from rllab.misc.instrument import stub, run_experiment_lite
from rllab.policies.gaussian_mlp_policy import GaussianMLPPolicy
from sandbox.pchen.diag_npg.optimizers.diagonal_natural_gradient_optimizer import DiagonalNaturalGradientOptimizer

# stub(globals())

env = normalize(CartpoleSwingupEnv())

policy = GaussianMLPPolicy(
    env_spec=env.spec,
    hidden_sizes=(32, 32)
)

baseline = LinearFeatureBaseline(env_spec=env.spec)

algo = NPO(
    env=env,
    policy=policy,
    baseline=baseline,
    batch_size=5000,
    max_path_length=500,
    n_itr=40,
    discount=0.99,
    step_size=0.01,
    optimizer=DiagonalNaturalGradientOptimizer(
        # mode="diag_hess",
        # mode="block_diag_hess",
        mode="cg_block_diag_hess",
        # mode="logprob_square",
    ),
)

run_experiment_lite(
    algo.train(),
    n_parallel=1,
    snapshot_mode="last",
    seed=1,
    mode="local",
)
