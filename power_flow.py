"""A module containing a power flow analysis API.

The power flow expects bus and line data to be provided in separate Excel worksheets. Once the analysis parameters and
data are provided, the following algorithm is executed:

    1. load bus data and set flat start voltages
    2. create admittance matrix from line data
    3. until a solution is found:
        3.1 generate real and reactive power error terms (dP and dQ)
        3.2 generate voltage and phase angle corrections through an iteration of the Newton-Raphson algorithm
            3.2.1 generate the Jacobian matrix (J = [[dP/dtheta, dP/dV], [dQ/dtheta, dQ/dV]])
            3.2.2 solve inv(J) * [[dP], [dQ]] to get a vector of voltage corrections ([[dtheta], [dV]])
        3.3 apply corrections to each bus (except the slack bus)

More formally, let S(actual) = P(actual) + jQ(actual) be a vector of power consumption at each bus, V be a vector of
voltages (and voltage angles) at each bus, and Y be the admittance matrix for the system. Initially, any unknown
voltages in V are set to a reference voltage (usually 1.0 pu, the flat start voltage), and the Newton-Raphson method is
used to reduce the error in bus power to zero, and iterate the system towards a steady-state solution.

    while true:
        P(calculated) = [sum(i = 1 to N) B[k][i] V[k] V[i] sin(theta[k] - theta[i])]
        Q(calculated) = [sum(i = 1 to N) -B[k][i] V[k] V[i] cos(theta[k] - theta[i])]
        P(error) = P(calculated) - P(actual)
        Q(error) = Q(calculated) - Q(actual)

        if P(error) < P(max error) and Q(error) < Q(max error):
            break

        x = [angle(V), abs(V)]
        J = [[dP(error)/dtheta, dP(error)/dV], [dQ(error)/dtheta, dQ(error)/dV]]
        dx = inverse(J) x

        V += dx[0] e^(j dx[1])

The algorithm is considered complete when the magnitude of the error is less than some specified allowable threshold
(max_error_mw and max_error_mvar).
"""

import namedlist
import numpy
import operator
import power_flow_jacobian

# An object containing bus data.
#
# Attributes:
#   number: The bus number.
#   power: The complex power consumed at the bus (pu).
#   voltage: The bus voltage (pu).
Bus = namedlist.namedlist('Bus', ['number', 'power', 'voltage'])


class PowerFlow():
    """A power flow object."""

    def __init__(
        self, bus_data, line_data, slack_bus_number, start_voltage, power_base_mva, max_error_mw, max_error_mvar):
        """Creates a new power flow object.

        Args:
            bus_data: The openpyxl worksheet containing bus data.
            line_data: The openpyxl worksheet containing line data.
            slack_bus_number: The slack bus number.
            start_voltage: The initial voltages to use at each bus.
            power_base_mva: The power base in MVA.
            max_error_mw: The maximum allowable real power mismatch for a solution to be considered final.
            max_error_mvar: The maximum allowable reactive power mismatch for a solution to be considered final.
        """
        self.bus_data = bus_data
        self.line_data = line_data
        self.slack_bus_number = slack_bus_number
        self.start_voltage = start_voltage
        self.power_base_mva = power_base_mva
        self.max_active_power_mismatch_pu = max_error_mw / power_base_mva
        self.max_reactive_power_mismatch_pu = max_error_mvar / power_base_mva

    def execute(self):
        """Executes a power flow analysis.

        Returns:
            An array of buses and their steady-state solutions.
        """
        buses = self.read_bus_data()
        admittances = self.read_admittance_matrix(len(buses))

        while True:
            # Compute power flow equations. Ignore the slack bus when computing the error term.
            s_calc = self.create_power_flow_equations(buses, admittances)
            s_error = [s - bus.power for s, bus in zip(s_calc, buses) if bus.number != self.slack_bus_number]
            if self.is_convergent(s_error):
                break

            # Compute Jacobian and its inverse.
            jacobian = power_flow_jacobian.create_jacobian_matrix(buses, admittances, s_calc, self.slack_bus_number)
            invjacobian = numpy.linalg.inv(jacobian)

            # Compute corrections (an augmented vector of voltage angles and voltage magnitudes).
            pq_error = [[s.real for s in s_error], [s.imag for s in s_error]]
            corrections = -numpy.matmul(invjacobian, numpy.concatenate(pq_error))

            # Apply corrections.
            self.apply_corrections(buses, corrections)

        return buses

    def count_rows(self, ws):
        """Counts the number of non-empty, contiguous rows in the worksheet.

        This method is necessary because openpyxl.worksheet.worksheet.max_row does not always correctly detect empty
        cells.

        Args:
            ws: The worksheet to read.

        Returns:
            The number of non-empty, contiguous rows.
        """
        n = 0
        for row in ws.iter_rows():
            if not row[0].value:
                break

            n += 1

        return n

    def read_bus_data(self):
        """Reads bus data from an input worksheet.

        The input data is expected to have the following columns:

            1. Bus number
            2. Real power consumed (MW)
            3. Reactive power consumed (Mvar)
            4. Real power delivered (MW)
            5. Voltage (pu)

        Returns:
            An array containing bus data.
        """
        s_total = 0j
        buses = []
        for i, row in enumerate(self.bus_data.iter_rows(row_offset=1, max_row=self.count_rows(self.bus_data))):
            bus_num = row[0].value
            p_load = row[1].value or 0
            q_load = row[2].value or 0
            p_gen = row[3].value or 0
            voltage = row[4].value or self.start_voltage
            voltage += 0j  # force the voltage value to be complex

            s = (p_gen - (p_load + 1j * q_load)) / self.power_base_mva
            s_total += s
            buses.append(Bus(bus_num, s, voltage))

        # Balance system power in the slack bus.
        buses = sorted(buses, key=operator.attrgetter('number'))
        buses[self.slack_bus_number - 1].power = -s_total
        return buses

    def read_admittance_matrix(self, num_buses):
        """Reads an admittance matrix from an input worksheet.

        The worksheet is expected to have a header row. Each subsequent row is expected to contain the following
        columns:

            1. Source bus
            2. Destination bus
            3. Total resistance (pu)
            4. Total reactance (pu)
            5. Total susceptance (pu)
            6. Maximum apparent power (MVA)

        Furthermore, it is expected line data is not duplicated and that the source and destination buses are
        different.

        Args:
            num_buses: The number of buses in the system.

        Returns:
            A square matrix containing line-to-line admittances.
        """
        admittances = numpy.zeros((num_buses, num_buses)) * 1j  # multiply by 1j to make values complex

        # Compute matrix entries.
        for row in self.line_data.iter_rows(row_offset=1, max_row=num_buses):
            # Subtract 1 since arrays start at 0, but buses start at 1.
            src = row[0].value - 1
            dst = row[1].value - 1

            rtotal = row[2].value
            xtotal = row[3].value
            btotal = row[4].value
            # fmax = row[5].value

            # Series and parallel impedances.
            y_series = 1 / (rtotal + 1j * xtotal)
            y_parallel = 1j * btotal / 2

            # Subtract series admittances from off-diagonal entries.
            admittances[src][dst] -= y_series
            admittances[dst][src] -= y_series

            # Add series and parallel admittances to diagonal entries.
            admittances[src][src] += y_series + y_parallel
            admittances[dst][dst] += y_series + y_parallel

        return admittances

    def create_power_flow_equations(self, buses, admittances):
        """Creates the power flow equations from a set of buses and admittances.

        For each bus, the power flow equations are:

            dPk = sum(i = 1 to N) Vk * Vi * Bki * sin(theta_k - theta_i)
            dQk = sum(i = 1 to N) -Vk * Vi * Bki * cos(theta_k - theta_i)

        N is the number of buses, Vi is the voltage magnitude at bus i, Bki is the line admittance from bus k to bus i,
        and theta_i is the voltage angle at bus i.

        Args:
            buses: The state of the system buses.
            admittances: The system admittances.

        Returns:
            An array of power flow equations in the same order as the input buses.
        """
        s_values = []
        for src in buses:
            s = 0j

            k = src.number - 1
            v_k = numpy.abs(src.voltage)
            theta_k = numpy.angle(src.voltage)

            for dst in buses:
                j = dst.number - 1
                v_j = numpy.abs(dst.voltage)
                theta_j = numpy.angle(dst.voltage)
                b_kj = admittances[k][j].imag

                p = v_k * v_j * b_kj * numpy.sin(theta_k - theta_j)
                q = -v_k * v_j * b_kj * numpy.cos(theta_k - theta_j)
                s += p + 1j * q

            s_values.append(s)

        return s_values

    def is_convergent(self, errors):
        """Checks if the power flow has converged to a solution.

        Args:
            errors: The power error terms.

        Returns:
            True if the power flow has converged to a solution; false otherwise.
        """
        for e in errors:
            if e.real >= self.max_active_power_mismatch_pu or e.imag >= self.max_reactive_power_mismatch_pu:
                return False

        return True

    def apply_corrections(self, buses, corrections):
        """Applies corrections to bus voltages.

        The corrections vector is expected not to have an update for the slack bus. The vector is expected to be sorted
        by bus number.

        Args:
            buses: The system buses being updated.
            corrections: A sorted vector of voltage angles followed by voltage magnitudes.
        """
        theta_corrections = corrections[0:len(corrections) // 2]
        voltage_corrections = corrections[len(corrections) // 2:]

        # Apply corrections.
        count = 0
        for bus in buses:
            # Do not update the slack bus.
            if bus.number == self.slack_bus_number:
                continue

            voltage = numpy.abs(bus.voltage) + voltage_corrections[count]
            theta = numpy.angle(bus.voltage) + theta_corrections[count]
            bus.voltage = voltage * numpy.exp(1j * theta)
            count += 1
