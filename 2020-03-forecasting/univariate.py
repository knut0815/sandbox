import argparse
import itertools
import multiprocessing
import os
import pickle
import random
from timeit import default_timer

import pyro
import pyro.distributions as dist
import pyro.poutine as poutine
import torch
from pyro.infer import NUTS, SVI, EnergyDistance, Trace_ELBO
from pyro.infer.autoguide import AutoDelta, AutoNormal
from pyro.infer.reparam import StableReparam, SymmetricStableReparam
from pyro.optim import ClippedAdam

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "univariate")
if not os.path.exists(RESULTS):
    os.makedirs(RESULTS)


def model(data=None, skew=None, num_samples=None):
    stability = pyro.sample("stability", dist.Uniform(0, 2))
    if skew is None:
        skew = pyro.sample("skew", dist.Uniform(-1, 1))
    scale = pyro.sample("scale", dist.LogNormal(0, 10))
    loc = pyro.sample("loc", dist.Normal(0, scale))
    with pyro.plate("plate", num_samples if data is None else len(data)):
        return pyro.sample("x", dist.Stable(stability, skew, scale, loc), obs=data)


def synthesize(stability, skew, num_samples):
    data = {
        "stability": torch.tensor(float(stability)),
        "skew": torch.tensor(float(skew)),
        "scale": torch.ones(()),
        "loc": torch.zeros(()),
    }
    with poutine.condition(data=data):
        return model(num_samples=num_samples)


METHODS = {}


def method(name):
    def decorator(fn):
        METHODS[name] = fn
        return fn
    return decorator


@method("SVI")
def svi(data, min_stability, skew):
    rep = StableReparam() if skew is None else SymmetricStableReparam()
    reparam_model = poutine.reparam(model, {"x": rep})
    guide = AutoNormal(reparam_model)
    num_steps = 1001
    optim = ClippedAdam({"lr": 0.05, "lrd": 0.1 ** (1 / num_steps), "betas": (0.8, 0.95)})
    svi = SVI(reparam_model, guide, optim, Trace_ELBO())
    for step in range(num_steps):
        loss = svi.step(data, skew)
        if __debug__ and step % 100 == 0:
            print("step {} loss = {:0.4g}".format(step, loss / data.numel()))
    median = guide.median()
    return {
        "stability": median["stability"].item(),
        "skew": median["skew"].item() if skew is None else 0.,
        "scale": median["scale"].item(),
        "loc": median["loc"].item(),
    }


@method("Energy")
def energy(data, min_stability, skew):
    guide = AutoDelta(model)
    num_steps = 501
    optim = ClippedAdam({"lr": 0.05, "lrd": 0.1 ** (1 / num_steps), "betas": (0.8, 0.95)})
    svi = SVI(model, guide, optim, EnergyDistance(num_particles=8))
    for step in range(num_steps):
        loss = svi.step(data, skew)
        if __debug__ and step % 50 == 0:
            print("step {} loss = {:0.4g}".format(step, loss / data.numel()))
    median = guide.median()
    return {
        "stability": median["stability"].item(),
        "skew": median["skew"].item() if skew is None else 0.,
        "scale": median["scale"].item(),
        "loc": median["loc"].item(),
    }


def evaluate(name, stability, skew, num_samples, seed):
    pyro.clear_param_store()
    pyro.set_rng_seed(seed)
    data = synthesize(stability, skew, num_samples)
    min_stability = min(1.0, stability - 0.25)

    time = -default_timer()
    pred = METHODS[name](data, min_stability, None if skew else skew)
    time += default_timer()

    truth = {"stability": stability, "skew": skew, "scale": 1., "loc": 0.}
    error = {k: abs(pred[k] - v) for k, v in truth.items()}
    return {
        "name": name,
        "num_samples": num_samples,
        "seed": seed,
        "truth": truth,
        "pred": pred,
        "error": error,
        "time": time,
    }


def _evaluate(args):
    print(*args)
    filename = os.path.join(RESULTS, "{}_{:0.1f}_{:0.1f}_{}_{}.pkl".format(*args))
    if os.path.exists(filename):
        with open(filename, "rb") as f:
            return pickle.load(f)
    result = evaluate(*args)
    with open(filename, "wb") as f:
        pickle.dump(result, f)
    return result


def main(args):
    pyro.enable_validation(__debug__)
    grid = itertools.product(args.method.split(","),
                             map(float, args.stability.split(",")),
                             map(float, args.skew.split(",")),
                             map(int, args.num_samples.split(",")),
                             range(args.num_seeds))
    grid = list(grid)
    if args.shuffle:
        pyro.set_rng_seed(args.shuffle)
        random.shuffle(grid)
    map_ = map if __debug__ else multiprocessing.Pool().map
    summary = list(map_(_evaluate, grid))
    with open(os.path.join(RESULTS, "summary.pkl"), "wb") as f:
        pickle.dump(summary, f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Univariate stable parameter fitting")
    parser.add_argument("--method", default=",".join(sorted(METHODS)))
    parser.add_argument("--num-samples", default="100,1000,10000")
    parser.add_argument("--stability", default="0.5,1.0,1.5,1.7,1.9")
    parser.add_argument("--skew", default="0.0,0.1,0.5,0.9")
    parser.add_argument("--num-seeds", default=50, type=int)
    parser.add_argument("--shuffle", default=0, type=int)
    args = parser.parse_args()
    try:
        main(args)
    except Exception as e:
        print(e)
        import pdb
        pdb.post_mortem(e.__traceback__)
