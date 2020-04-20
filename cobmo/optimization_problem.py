"""Optimization problem module."""

import pandas as pd
import pyomo.environ as pyo
import time as time

import cobmo.config

logger = cobmo.config.get_logger(__name__)


class OptimizationProblem(object):
    """Optimization problem object."""

    def __init__(
            self,
            building,
            problem_type='operation',
            # Choices: 'operation', 'storage_planning', 'storage_planning_baseline', 'load_reduction',
            # 'price_sensitivity', 'maximum_load', 'minimum_load'
            output_vector_reference=None,
            load_reduction_start_time=None,
            load_reduction_end_time=None,
            price_sensitivity_factor=None,
            price_sensitivity_timestep=None,
    ):
        time_start = time.clock()
        self.building = building
        self.problem_type = problem_type
        self.output_vector_reference = output_vector_reference
        self.load_reduction_start_time = load_reduction_start_time
        self.load_reduction_end_time = load_reduction_end_time
        self.price_sensitivity_factor = price_sensitivity_factor
        self.price_sensitivity_timestep = price_sensitivity_timestep

        # Copy `electricity_price_timeseries` to allow local modifications.
        self.electricity_price_timeseries = self.building.electricity_price_timeseries.copy()

        self.problem = pyo.ConcreteModel()
        self.solver = pyo.SolverFactory(cobmo.config.solver_name)
        self.result = None

        # Define variables.
        self.problem.variable_state_vector = pyo.Var(
            self.building.timesteps,
            self.building.states,
            domain=pyo.Reals
        )
        self.problem.variable_control_vector = pyo.Var(
            self.building.timesteps,
            self.building.controls,
            domain=pyo.NonNegativeReals  # TODO: Workaround for proper behavior of battery storage.
        )
        self.problem.variable_output_vector = pyo.Var(
            self.building.timesteps,
            self.building.outputs,
            domain=pyo.Reals
        )
        if self.problem_type == 'storage_planning':
            self.problem.variable_storage_size = pyo.Var(
                [0],
                domain=pyo.NonNegativeReals
            )
            self.problem.variable_storage_peak_power = pyo.Var(
                [0],
                domain=pyo.NonNegativeReals
            )
            # Variable to describe if storage exists (= 1) or not (= 0).
            self.problem.variable_storage_exists = pyo.Var(
                [0],
                domain=pyo.Binary
            )
        if self.problem_type == 'storage_planning_baseline':
            # Force storage size to zero for baseline case.
            self.problem.variable_storage_size = [0.0]
        if self.problem_type == 'load_reduction':
            self.problem.variable_load_reduction = pyo.Var(
                [0],
                domain=pyo.NonNegativeReals
            )

        # Define constraints.
        self.problem.constraints = pyo.ConstraintList()

        # Initial state constraint.
        for state in self.building.states:
            self.problem.constraints.add(
                self.problem.variable_state_vector[self.building.timesteps[0], state]
                ==
                self.building.state_vector_initial[state]
            )

        # State equation constraint.
        # TODO: Move timestep_delta into building model.
        timestep_delta = self.building.timesteps[1] - self.building.timesteps[0]
        for state in self.building.states:
            for timestep in self.building.timesteps[:-1]:
                self.problem.constraints.add(
                    self.problem.variable_state_vector[timestep + timestep_delta, state]
                    ==
                    (
                        sum(
                            self.building.state_matrix.loc[state, state_other]
                            * self.problem.variable_state_vector[timestep, state_other]
                            for state_other in self.building.states
                        )
                        + sum(
                            self.building.control_matrix.loc[state, control]
                            * self.problem.variable_control_vector[timestep, control]
                            for control in self.building.controls
                        )
                        + sum(
                            self.building.disturbance_matrix.loc[state, disturbance]
                            * self.building.disturbance_timeseries.loc[timestep, disturbance]
                            for disturbance in self.building.disturbances
                        )
                    )
                )

        # Output equation constraint.
        for output in self.building.outputs:
            for timestep in self.building.timesteps:
                self.problem.constraints.add(
                    self.problem.variable_output_vector[timestep, output]
                    ==
                    (
                        sum(
                            self.building.state_output_matrix.loc[output, state]
                            * self.problem.variable_state_vector[timestep, state]
                            for state in self.building.states
                        )
                        + sum(
                            self.building.control_output_matrix.loc[output, control]
                            * self.problem.variable_control_vector[timestep, control]
                            for control in self.building.controls
                        )
                        + sum(
                            self.building.disturbance_output_matrix.loc[output, disturbance]
                            * self.building.disturbance_timeseries.loc[timestep, disturbance]
                            for disturbance in self.building.disturbances
                        )
                    )
                )

        # Output minimum / maximum bounds constraint.
        for output in self.building.outputs:
            for timestep in self.building.timesteps:
                # Minimum.
                # if running maximum load problem, set the temperature at each timestep to be the minimum allowed,
                # except for the initial timestep
                if ('temperature' in output) and (self.problem_type == 'maximum_load'):
                    if timestep == self.building.timesteps[0]:
                        pass
                    else:
                        self.problem.constraints.add(
                            self.problem.variable_output_vector[timestep, output]
                            ==
                            self.building.output_constraint_timeseries_minimum.loc[timestep, output]
                        )
                else:
                    self.problem.constraints.add(
                        self.problem.variable_output_vector[timestep, output]
                        >=
                        self.building.output_constraint_timeseries_minimum.loc[timestep, output]
                    )

                # Maximum.
                if (
                    ((self.problem_type == 'storage_planning') or (self.problem_type == 'storage_planning_baseline'))
                    and ('state_of_charge' in output)
                ):
                    # Storage planning constraints.
                    if 'sensible' in self.building.building_data.scenarios['building_storage_type']:
                        self.problem.constraints.add(
                            self.problem.variable_output_vector[timestep, output]
                            <=
                            self.problem.variable_storage_size[0]
                            * self.building.parse_parameter('water_density')
                        )
                    elif 'battery' in self.building.building_data.scenarios['building_storage_type']:
                        self.problem.constraints.add(
                            self.problem.variable_output_vector[timestep, output]
                            <=
                            self.problem.variable_storage_size[0]
                            * self.building.building_data.scenarios['storage_battery_depth_of_discharge']
                        )
                else:
                    self.problem.constraints.add(
                        self.problem.variable_output_vector[timestep, output]
                        <=
                        self.building.output_constraint_timeseries_maximum.loc[timestep, output]
                    )

        # Storage planning auxiliary constraints.
        if self.problem_type == 'storage_planning':
            for timestep in self.building.timesteps:
                # Storage peak demand constraint.
                self.problem.constraints.add(
                    sum(
                        self.problem.variable_output_vector[timestep, output]
                        if ('storage_charge' in output) and ('electric_power' in output) else 0.0
                        for output in self.building.outputs
                    )
                    <=
                    self.problem.variable_storage_peak_power[0]
                )

                # Storage existence constraint.
                self.problem.constraints.add(
                    self.problem.variable_storage_size[0]
                    <=
                    self.problem.variable_storage_exists[0]
                    * 1.0e100  # Large constant as replacement for infinity.
                )

        # Demand side flexibility auxiliary constraints.
        elif self.problem_type == 'load_reduction':
            for timestep in self.building.timesteps:
                if (
                    (timestep >= self.load_reduction_start_time)
                    and (timestep < self.load_reduction_end_time)
                ):
                    # TODO: Introduce total electric demand in building outputs.
                    self.problem.constraints.add(
                        sum(
                            self.problem.variable_output_vector[timestep, output]
                            if (('electric_power' in output) and not ('storage_to_zone' in output)) else 0.0
                            for output in self.building.outputs
                        )
                        ==
                        (
                            (1.0 - (self.problem.variable_load_reduction[0] / 100.0))
                            * sum(
                                self.output_vector_reference.loc[timestep, output]
                                if (('electric_power' in output) and not ('storage_to_zone' in output)) else 0.0
                                for output in self.building.outputs
                            )
                        )
                    )

        # Define components of the objective.
        self.operation_cost = 0.0
        self.investment_cost = 0.0

        # Operation cost factor.
        if (self.problem_type == 'storage_planning') or (self.problem_type == 'storage_planning_baseline'):
            # Define operation cost factor to scale operation cost to the lifetime of storage.
            self.operation_cost_factor = (
                (pd.to_timedelta('1y') / pd.to_timedelta(timestep_delta))  # Theoretical number of time steps in a year.
                / len(self.building.timesteps)  # Actual number of time steps.
                * self.building.building_data.scenarios['storage_lifetime']  # Storage lifetime in years.
                * 14.0  # 14 levels at CREATE Tower. # TODO: Check if considered properly in storage size.
            )
        elif self.problem_type == 'load_reduction':
            # Adjust weight of operation cost when running load reduction problem.
            # - Workaround for unrealistic demand when not considering operation cost at all.
            # - This is a tuning parameter (has impact on load reduction result).
            self.operation_cost_factor = 1.0e-6
        else:
            # No scaling needed if not running planning problem.
            self.operation_cost_factor = 1.0

        # Modify price for price sensitivity evaluation.
        if self.problem_type == 'price_sensitivity':
            self.electricity_price_timeseries.at[self.price_sensitivity_timestep, 'price'] *= (
                self.price_sensitivity_factor
            )

        # Operation cost (OPEX).
        for timestep in self.building.timesteps:
            for output in self.building.outputs:
                if self.problem_type in ['minimum_load', 'maximum_load']:
                    if output == 'grid_electric_power':
                        self.operation_cost += self.problem.variable_output_vector[timestep, output]
                else:
                    if ('electric_power' in output) and not ('storage_to_zone' in output):
                        self.operation_cost += (
                            self.problem.variable_output_vector[timestep, output]
                            * timestep_delta.seconds / 3600.0 / 1000.0  # W in kWh.
                            * self.electricity_price_timeseries.loc[timestep, 'price']
                            * self.operation_cost_factor
                        )

        # Investment cost (CAPEX).
        if self.problem_type == 'storage_planning':
            if 'sensible' in self.building.building_data.scenarios['building_storage_type']:
                self.investment_cost += (
                    self.problem.variable_storage_size[0]  # In m3.
                    * self.building.building_data.scenarios['storage_planning_energy_installation_cost']  # In SGD/m3.
                    # TODO: Currently, power / fixed cost are set to zero for sensible thermal storage in the database.
                    + self.problem.variable_storage_peak_power[0] / 1000.0  # W in kW.
                    * self.building.building_data.scenarios['storage_planning_power_installation_cost']  # In SGD/kW.
                    + self.problem.variable_storage_exists[0]  # No unit.
                    * self.building.building_data.scenarios['storage_planning_fixed_installation_cost']  # In SGD.
                )
            elif 'battery' in self.building.building_data.scenarios['building_storage_type']:
                self.investment_cost += (
                    self.problem.variable_storage_size[0] / 3600.0 / 1000.0  # Ws in kWh (J in kWh).
                    * self.building.building_data.scenarios['storage_planning_energy_installation_cost']
                    # TODO: Validate unit of power cost.
                    + self.problem.variable_storage_peak_power[0] / 1000.0  # W in kW.
                    * self.building.building_data.scenarios['storage_planning_power_installation_cost']  # In SGD/kW
                    + self.problem.variable_storage_exists[0]  # No unit.
                    * self.building.building_data.scenarios['storage_planning_fixed_installation_cost']  # In SGD.
                )
        elif self.problem_type == 'load_reduction':
            # TODO: Introduce dedicated cost for demand side flexibility indicators.
            self.investment_cost -= self.problem.variable_load_reduction[0]  # In percent.

        # Define objective.
        self.problem.objective = pyo.Objective(
            expr=(self.operation_cost + self.investment_cost),
            sense=pyo.minimize
        )

        # Print setup time for debugging.
        logger.debug("OptimizationProblem setup time: {:.2f} seconds".format(time.clock() - time_start))

    def solve(self):
        """Invoke solver on Pyomo problem."""

        # Solve problem.
        time_start = time.clock()
        self.result = self.solver.solve(
            self.problem,
            tee=cobmo.config.solver_output  # If True, activate verbose solver output.
        )

        # Print solve time for debugging.
        logger.debug("OptimizationProblem solve time: {:.2f} seconds".format(time.clock() - time_start))

        # Retrieve results.
        time_start = time.clock()
        control_vector = pd.DataFrame(
            0.0,
            self.building.timesteps,
            self.building.controls
        )
        state_vector = pd.DataFrame(
            0.0,
            self.building.timesteps,
            self.building.states
        )
        output_vector = pd.DataFrame(
            0.0,
            self.building.timesteps,
            self.building.outputs
        )
        for timestep in self.building.timesteps:
            for control in self.building.controls:
                control_vector.at[timestep, control] = (
                    self.problem.variable_control_vector[timestep, control].value
                )
            for state in self.building.states:
                state_vector.at[timestep, state] = (
                    self.problem.variable_state_vector[timestep, state].value
                )
            for output in self.building.outputs:
                output_vector.at[timestep, output] = (
                    self.problem.variable_output_vector[timestep, output].value
                )

        # Retrieve objective / cost values.
        if type(self.operation_cost) is float:
            operation_cost = self.operation_cost
        else:
            operation_cost = pyo.value(self.operation_cost)
        if type(self.investment_cost) is float:
            investment_cost = self.investment_cost
        else:
            investment_cost = pyo.value(self.investment_cost)

        # Retrieve storage size.
        if self.problem_type == 'storage_planning':
            storage_size = self.problem.variable_storage_size[0].value
        elif self.problem_type == 'storage_planning_baseline':
            storage_size = self.problem.variable_storage_size[0]
        else:
            storage_size = None

        # Print results compilation time for debugging.
        logger.debug("OptimizationProblem results compilation time: {:.2f} seconds".format(time.clock() - time_start))

        return (
            control_vector,
            state_vector,
            output_vector,
            operation_cost,
            investment_cost,
            storage_size
        )
