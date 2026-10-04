"""Helmod / Factory Planner style production solver over final prototypes.

Two solvers share one line model (recipe + machine + modules + beacons):

* ``lp``     – linear program (HiGHS). Picks among alternative recipes, allows
               raw imports and penalised surplus, minimises a weighted objective
               (machines / power / imports). Returns shadow prices per item.
* ``matrix`` – exact linear algebra (Factory Planner "matrix solver"). One
               unknown per line plus import/surplus unknowns chosen by item role;
               reports rank, degrees of freedom, null space and inconsistent items
               instead of silently guessing.

Effects follow the 2.0 prototype docs: module effects + beacon effects
(distribution_effectivity x profile[beacon count]) + machine base_effect + force
recipe productivity; speed/consumption/pollution multipliers are clamped at 20%,
productivity at [0, maximum_productivity] and zeroed when the recipe disallows it.
Electric drain defaults to energy_usage/30 for crafting machines. Quality,
surface effects, fluid-resource yield depletion and belt/pipe throughput are not
modelled. Rates are expected values (probability and amount ranges averaged).
"""
from .api import machine_stats, plan, production_matrix
from .model import Planner, product_amount, watts

__all__ = ['Planner', 'machine_stats', 'plan', 'product_amount', 'production_matrix', 'watts']
