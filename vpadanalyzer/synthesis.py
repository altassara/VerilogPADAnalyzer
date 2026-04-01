import re
import subprocess
from subprocess import PIPE
import time
import os
from . import config
from .paths import OPENSTA, YOSYS

class Synthesis:
    def __init__(self, input_path: str, temp_dir: str = None, report_dir: str = None):
        self._input_path = input_path
        self._module_name = self.__get_module_name()
        self._temp_dir = f'{temp_dir}' if temp_dir else 'temp'
        self._rep_dir = f'{report_dir}' if report_dir else 'report'
        os.makedirs(self._temp_dir, exist_ok=True)
        os.makedirs(self._rep_dir, exist_ok=True)
        self._syn_path = f'{self._temp_dir}/{self._module_name}_syn.v'
        self._power_script = f'{self._temp_dir}/{self._module_name}_power.script'
        self._delay_script = f'{self._temp_dir}/{self._module_name}_delay.script'

        self._area = None
        self._power = None
        self._delay = None

        self._config_path = self.get_config_path()
        #self._lib_path = f'{self._config_path}/gscl45nm.lib'
        self._lib_path = f'syn_lib/nangate_45nm_typ.lib'
        self._verilog_lib_path = f'syn_lib/cells.v'
        self._abc_script_path = f'{self._config_path}/abc.script'


    # =========================

    def get_config_path(self):
        init_address = os.path.abspath(config.__file__)
        init_address = init_address.replace('__init__.py', "")
        return init_address
    def get_area(self) -> float:
        """
        measures the area with yosys synthesis tool with the tech. library specified
        :return: a float number representing the area
        """
        yosys_command = f"read_verilog \"{self._input_path}\";\n" \
                        f"synth -flatten;\n" \
                        f"opt;\n" \
                        f"opt_clean -purge;\n" \
                        f"abc -liberty {self._lib_path} -script {self._abc_script_path};\n" \
                        f"stat -liberty {self._lib_path};\n"

        process = subprocess.run([YOSYS, '-p', yosys_command], stdout=PIPE, stderr=PIPE)
        if process.stderr:
            raise Exception(f'Yosys ERROR!!!\n {process.stderr.decode()}')
        else:

            if re.search(r'Chip area for .*: (\d+.\d+)', process.stdout.decode()):
                area = re.search(r'Chip area for .*: (\d+.\d+)', process.stdout.decode()).group(1)

            elif re.search(r"Don't call ABC as there is nothing to map", process.stdout.decode()):
                area = 0
            else:
                raise Exception('Yosys ERROR!!!\nNo useful information in the stats log!')


        with open(f'{self._rep_dir}/{self._module_name}.area', 'w') as a:
            a.write(f'{float(area)}\n')
            self._area = float(area)
        return float(area)

    def get_power(self, input_vectors_file=None, per_vector_vcd=False, distribution=None):
        """
        measures the power with opensta synthesis tool with the tech. library specified
        If input_vectors_file is provided (path to a file containing input vectors), runs a VCD simulation first.

        Args:
            input_vectors_file: text file with one binary vector per non-empty line.
            per_vector_vcd: if True, generates one VCD for each line in input_vectors_file.
            distribution:
                - None -> uniform average over all vectors
                - numpy ndarray 2D or .npy file path -> probability matrix
        """
        self.__synthesize()

        if input_vectors_file and per_vector_vcd:
            entries = self.__run_vcd_batch_simulation(input_vectors_file)
            if not entries:
                raise Exception("No valid vectors found in input file.")

            vectors = [vector for _, vector, _, _ in entries]
            weights = self.__resolve_distribution_weights(vectors, distribution)

            weighted_sum = 0.0
            total_weight = 0.0
            powers_rows = []
            for idx, vector, power_value, vcd_file in entries:
                w = weights[idx]
                weighted_sum += power_value * w
                total_weight += w
                powers_rows.append((idx, vector, power_value, w, vcd_file))

            if total_weight <= 0:
                raise Exception("Invalid distribution: total weight is 0.")

            avg_power = weighted_sum / total_weight

            with open(f'{self._rep_dir}/{self._module_name}.power', 'w') as a:
                a.write(f'{float(avg_power)}\n')

            details_path = f'{self._rep_dir}/{self._module_name}_power_per_vector.csv'
            with open(details_path, 'w') as f:
                f.write('index,vector,power,weight,vcd_path\n')
                for idx, vector, power_value, weight, vcd_path in powers_rows:
                    f.write(f'{idx},{vector},{power_value},{weight},{vcd_path}\n')

            self._power = float(avg_power)
            return float(avg_power)

        cmds = []
        cmds.append(f"read_liberty {self._lib_path}")
        cmds.append(f"read_verilog {self._syn_path}")
        cmds.append(f"link_design {self._module_name}")
        cmds.append(f"create_clock -name clk -period 1")
        
        # Gestione VCD Simulation
        if input_vectors_file:
            vcd_file = self.__run_vcd_simulation(input_vectors_file)
            if vcd_file and os.path.exists(vcd_file):
                cmds.append(f"read_vcd -scope tb_{self._module_name}/uut {vcd_file}")
            else:
                print("Warning: VCD simulation failed, falling back to default activity.")
                cmds.append(f"set_input_delay -clock clk 0 [all_inputs]")
                cmds.append(f"set_output_delay -clock clk 0 [all_outputs]")
        else:
             cmds.append(f"set_input_delay -clock clk 0 [all_inputs]")
             cmds.append(f"set_output_delay -clock clk 0 [all_outputs]")
        
        cmds.append("report_checks")
        cmds.append("report_power -digits 12")
        cmds.append("exit")
        
        sta_command = "\n".join(cmds) + "\n"

        with open(self._power_script, 'w') as ds:
            ds.writelines(sta_command)
        # process = subprocess.run([sxpatconfig.OPENSTA, power_script], stderr=PIPE)

        process = subprocess.run([OPENSTA, self._power_script], stdout=PIPE, stderr=PIPE)

        # DEBUG: Save OpenSTA log for inspection
        with open(f'{self._rep_dir}/{self._module_name}_opensta_debug.log', 'w') as log:
             log.write(f"CMD: {OPENSTA} {self._power_script}\n")
             if process.stdout: log.write(process.stdout.decode())
             if process.stderr: log.write("\nSTDERR:\n" + process.stderr.decode())

        if process.stderr:
            # raise Exception(f'OpenSTA ERROR!!!\n {process.stderr.decode()}')
            pass
        else:
            total_power = self.__extract_total_power_from_opensta_output(process.stdout.decode())
            with open(f'{self._rep_dir}/{self._module_name}.power', 'w') as a:
                a.write(f'{float(total_power)}\n')
            self._power = float(total_power)
            return float(total_power)


    def __resolve_distribution_weights(self, vectors, distribution):
        if distribution is None:
            return [1/len(vectors)] * len(vectors)

        matrix = self.__load_distribution_matrix(distribution)
        rows = len(matrix)
        cols = len(matrix[0]) if rows > 0 else 0
        if rows <= 0 or cols <= 0:
            raise Exception("Distribution matrix must be 2D and non-empty.")

        bits_a = (rows - 1).bit_length()
        bits_b = (cols - 1).bit_length()
        expected_bits = bits_a + bits_b

        if (1 << bits_a) != rows or (1 << bits_b) != cols:
            raise Exception("Distribution matrix dimensions must be powers of 2 (e.g. 256x256).")

        out = []
        for v in vectors:
            vv = v.strip()
            if len(vv) != expected_bits:
                raise Exception(
                    f"Vector length mismatch: expected {expected_bits}, got {len(vv)} for vector '{v}'"
                )
            a_idx = int(vv[:bits_a], 2)
            b_idx = int(vv[bits_a:], 2)
            out.append(float(matrix[a_idx][b_idx]))
        return out


    def __load_distribution_matrix(self, distribution):
        try:
            import numpy as np
        except Exception as e:
            raise Exception(f"NumPy is required for distribution matrices: {e}")

        if isinstance(distribution, str):
            if not os.path.exists(distribution):
                raise Exception(f"Distribution file not found: {distribution}")
            matrix = np.load(distribution)
        else:
            matrix = distribution

        if not hasattr(matrix, 'shape') or len(matrix.shape) != 2:
            raise Exception("Distribution must be a 2D numpy matrix or a .npy file path.")

        return matrix


    def __extract_total_power_from_opensta_output(self, output_text):
        pattern = r"Total\s+(\d+.\d+)[^0-9]*\d+\s+(\d+.\d+)[^0-9]*\d+\s+(\d+.\d+)[^0-9]*\d+\s+(\d+.\d+[^0-9]*\d+)\s+"
        if not re.search(pattern, output_text):
            print('OpenSTA Warning! Design has 0 power consumption!')
            return 0.0

        total_power_str = re.search(pattern, output_text).group(4)
        if re.search(r'e[^0-9]*(\d+)', total_power_str):
            total_power = float(re.search(r'(\d+.\d+)e[^0-9]*\d+', total_power_str).group(1))
            sign = re.search(r'e([^0-9]*)(\d+)', total_power_str).group(1)
            sign = -1 if sign == '-' else +1
            exponent = int(re.search(r'e([^0-9]*)(\d+)', total_power_str).group(2))
            total_power = total_power * (10 ** (sign * exponent))
            return float(total_power)
        return float(total_power_str)


    def __measure_power_from_vcd(self, vcd_file):
        cmds = []
        cmds.append(f"read_liberty {self._lib_path}")
        cmds.append(f"read_verilog {self._syn_path}")
        cmds.append(f"link_design {self._module_name}")
        cmds.append(f"create_clock -name clk -period 1")
        cmds.append(f"read_vcd -scope tb_{self._module_name}/uut {vcd_file}")
        cmds.append("report_checks")
        cmds.append("report_power -digits 12")
        cmds.append("exit")
        sta_command = "\n".join(cmds) + "\n"

        script_path = f'{self._temp_dir}/{self._module_name}_power_batch.script'
        with open(script_path, 'w') as ds:
            ds.writelines(sta_command)

        process = subprocess.run([OPENSTA, script_path], stdout=PIPE, stderr=PIPE)

        debug_log = f'{self._rep_dir}/{self._module_name}_opensta_batch_debug.log'
        with open(debug_log, 'w') as log:
            log.write(f"CMD: {OPENSTA} {script_path}\n")
            if process.stdout:
                log.write(process.stdout.decode())
            if process.stderr:
                log.write("\nSTDERR:\n" + process.stderr.decode())

        return self.__extract_total_power_from_opensta_output(process.stdout.decode())


    def __run_vcd_batch_simulation(self, input_vectors_file):
        """
        Generates exactly one testbench and one compiled simulation binary,
        then runs one simulation per input vector line reusing a single runtime VCD file.
        Power is measured immediately after each simulation.
        """
        if not (isinstance(input_vectors_file, str) and os.path.exists(input_vectors_file)):
            raise Exception("Input vectors must be a valid file path.")

        vectors = []
        with open(input_vectors_file, 'r') as f:
            for line in f:
                vec = line.strip()
                if vec:
                    vectors.append(vec)

        if not vectors:
            raise Exception("Input vectors file is empty or contains only blank lines.")

        inputs, outputs = self.__extract_ports()

        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

        inputs.sort(key=natural_sort_key)

        num_inputs = len(inputs)
        tb_file = f'{self._temp_dir}/tb_{self._module_name}.v'
        sim_out = f'{self._temp_dir}/sim_{self._module_name}.out'
        runtime_input_file = f'{self._temp_dir}/{self._module_name}_runtime_input.mem'
        runtime_vcd_file = f'{self._temp_dir}/{self._module_name}_runtime.vcd'

        with open(tb_file, 'w') as tb:
            tb.write("`timescale 1ns/1ps\n")
            tb.write(f"module tb_{self._module_name};\n")
            for p in inputs:
                p_fmt = f"{p} " if p.startswith('\\') else p
                tb.write(f"  reg {p_fmt};\n")
            for p in outputs:
                p_fmt = f"{p} " if p.startswith('\\') else p
                tb.write(f"  wire {p_fmt};\n")

            tb.write("  reg clk;\n")
            tb.write(f"  reg [{num_inputs}-1:0] test_vectors [0:0];\n")

            tb.write(f"  {self._module_name} uut (\n")
            connections = []
            for p in inputs + outputs:
                p_fmt = f"{p} " if p.startswith('\\') else p
                connections.append(f"    .{p_fmt}({p_fmt})")
            tb.write(",\n".join(connections))
            tb.write("\n  );\n")

            tb.write("  initial begin\n    clk = 0;\n    forever #1 clk = ~clk;\n  end\n")
            tb.write("  initial begin\n")
            tb.write(f"    $readmemb(\"{runtime_input_file}\", test_vectors);\n")
            tb.write(f"    $dumpfile(\"{runtime_vcd_file}\");\n")
            tb.write(f"    $dumpvars(0, tb_{self._module_name});\n")
            tb.write("    @(negedge clk);\n")
            if inputs:
                fmt_concat = [f"{p} " if p.startswith('\\') else p for p in inputs]
                tb.write(f"    {{ {', '.join(fmt_concat)} }} = test_vectors[0];\n")
            tb.write("    @(negedge clk);\n")
            tb.write("    $finish;\n")
            tb.write("  end\n")
            tb.write("endmodule\n")

        lib_verilog = self._verilog_lib_path
        cmd_compile = ['iverilog', '-o', sim_out, tb_file, self._syn_path, lib_verilog]
        subprocess.run(cmd_compile, check=True, stdout=PIPE, stderr=PIPE)
        print(f"[DEBUG] Compilazione testbench completata. Vettori da simulare: {len(vectors)}")

        out_entries = []
        for idx, vector in enumerate(vectors):
            with open(runtime_input_file, 'w') as f:
                f.write(vector + "\n")

            cmd_run = ['vvp', sim_out]
            subprocess.run(cmd_run, check=True, stdout=PIPE, stderr=PIPE)
            print(f"[DEBUG] Finita simulazione {idx + 1}/{len(vectors)}")

            current_vcd = runtime_vcd_file
            power_value = self.__measure_power_from_vcd(current_vcd)
            out_entries.append((idx, vector, power_value, current_vcd))

        return out_entries


    def __extract_ports(self):
        """
        Parses the synthesized Verilog file to find extracted ports.
        Returns two lists: inputs and outputs.
        Handles both 'input \A[0] ;' and 'input in0;' formats.
        """
        inputs = []
        outputs = []
        
        with open(self._syn_path, 'r') as f:
            content = f.read()
            
        # Regex per trovare dichiarazioni input/output
        # Cerca: "input " seguito da nome porta (che potrebbe avere backslash o parentesi) e punto e virgola
        input_matches = re.findall(r'input\s+([^;]+);', content)
        output_matches = re.findall(r'output\s+([^;]+);', content)
        
        # Pulizia dei nomi (rimuove spazi e newlines extra)
        for i_grp in input_matches:
            # Potrebbero esserci più porte sulla stessa riga separate da virgola
            ports = [p.strip() for p in i_grp.split(',')]
            inputs.extend(ports)
            
        for o_grp in output_matches:
            ports = [p.strip() for p in o_grp.split(',')]
            outputs.extend(ports)
            
        return inputs, outputs


    def __run_vcd_simulation(self, input_vectors):
        """
        Generates a testbench, runs iverilog simulation, and returns path to VCD file.
        input_vectors: Path to file only.
                       Each string represents the full concave of A and B bits.
                       Assumes MSB is first char in string.
        """
        
        inputs, outputs = self.__extract_ports()
        
        
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower()
                    for text in re.split('([0-9]+)', s)]
        
        inputs.sort(key=natural_sort_key)
        
        num_inputs = len(inputs)
        
        tb_file = f'{self._temp_dir}/tb_{self._module_name}.v'
        vcd_file = f'{self._temp_dir}/{self._module_name}.vcd'
        
        num_vectors = 0
        if isinstance(input_vectors, str) and os.path.exists(input_vectors):
            input_data_file = os.path.abspath(input_vectors)
            with open(input_data_file, 'r') as f:
                num_vectors = sum(1 for line in f if line.strip())
        else:
            raise Exception("Input vectors must be a valid file path.")
        

        with open(tb_file, 'w') as tb:
            tb.write(f"`timescale 1ns/1ps\n")
            tb.write(f"module tb_{self._module_name};\n")
            
            # Dichiarazione Reg/Wire
            for p in inputs:
                p_fmt = f"{p} " if p.startswith('\\') else p
                tb.write(f"  reg {p_fmt};\n")
            for p in outputs:
                p_fmt = f"{p} " if p.startswith('\\') else p
                tb.write(f"  wire {p_fmt};\n")
            
            tb.write("  reg clk;\n")
            
            # Memoria per vettori
            tb.write(f"  reg [{num_inputs}-1:0] test_vectors [0:{num_vectors-1}];\n")
            tb.write("  integer i;\n")
            
            # Istanza UUT
            tb.write(f"  {self._module_name} uut (\n")
            connections = []
            for p in inputs + outputs:
                p_fmt = f"{p} " if p.startswith('\\') else p
                connections.append(f"    .{p_fmt}({p_fmt})")
            tb.write(",\n".join(connections))
            tb.write("\n  );\n")
            
            # Clock Generation
            tb.write("  initial begin\n    clk = 0;\n    forever #1 clk = ~clk;\n  end\n")
            
            # Stimulus Process
            tb.write("  initial begin\n")
            tb.write(f"    $readmemb(\"{input_data_file}\", test_vectors);\n")
            tb.write(f"    $dumpfile(\"{vcd_file}\");\n")
            tb.write(f"    $dumpvars(0, tb_{self._module_name});\n")
            
            tb.write(f"    for (i=0; i<{num_vectors}; i=i+1) begin\n")
            tb.write("      @(negedge clk);\n")
            
            # Il bit più a sinistra (index N-1) è l'ultimo input nel sort (B[7]).
            # Il bit più a destra (index 0) è il primo input nel sort (A[0]).
            
            concat_list = list(inputs)
            if concat_list:
                # Format each port in the concatenation list
                fmt_concat = [f"{p} " if p.startswith('\\') else p for p in concat_list]
                tb.write(f"      {{ {', '.join(fmt_concat)} }} = test_vectors[i];\n")
            
            tb.write("    end\n")
            tb.write("    @(negedge clk);\n")
            tb.write("    $finish;\n")
            tb.write("  end\n")
            tb.write("endmodule\n")

        sim_out = f'{self._temp_dir}/sim_{self._module_name}.out'
        lib_verilog = self._verilog_lib_path
        
        try:
            cmd_compile = ['iverilog', '-o', sim_out, tb_file, self._syn_path, lib_verilog]
            subprocess.run(cmd_compile, check=True, stdout=PIPE, stderr=PIPE)
            
            cmd_run = ['vvp', sim_out]
            subprocess.run(cmd_run, check=True, stdout=PIPE, stderr=PIPE)
        except subprocess.CalledProcessError as e:
            error_msg = f"Error running command: {e.cmd}\nReturn code: {e.returncode}\n"
            if e.stdout:
                error_msg += f"Stdout:\n{e.stdout.decode()}\n"
            if e.stderr:
                error_msg += f"Stderr:\n{e.stderr.decode()}\n"
            raise Exception(error_msg)
        
        # Cleanup
        #if os.path.exists(sim_out): os.remove(sim_out)
        #if os.path.exists(tb_file): os.remove(tb_file)
        # if os.path.exists(input_data_file): os.remove(input_data_file)
        
        return vcd_file



    def get_delay(self):
        """
        measures the delay with opensta synthesis tool with the tech. library specified
        :return: a float number representing the delay
        """
        self.__synthesize()

        sta_command = f"read_liberty {self._lib_path}\n" \
                      f"read_verilog {self._syn_path}\n" \
                      f"link_design {self._module_name}\n" \
                      f"create_clock -name clk -period 1\n" \
                      f"set_input_delay -clock clk 0 [all_inputs]\n" \
                      f"set_output_delay -clock clk 0 [all_outputs]\n" \
                      f"report_checks -digits 6\n" \
                      f"exit"
        with open(self._delay_script, 'w') as ds:
            ds.writelines(sta_command)
        # process = subprocess.run([sxpatconfig.OPENSTA, delay_script], stderr=PIPE)
        process = subprocess.run([OPENSTA, self._delay_script], stdout=PIPE, stderr=PIPE)
        if process.stderr:
            raise Exception(f'Yosys ERROR!!!\n {process.stderr.decode()}')
        else:
            os.remove(self._delay_script)
            if re.search('(\d+.\d+).*data arrival time', process.stdout.decode()):
                time = re.search('(\d+.\d+).*data arrival time', process.stdout.decode()).group(1)
                with open(f'{self._rep_dir}/{self._module_name}.delay', 'w') as a:
                    a.write(f'{float(time)}\n')
                self._delay = float(time)
                return float(time)
            else:
                print('OpenSTA Warning! Design has 0 delay!')
                with open(f'{self._rep_dir}/{self._module_name}.delay', 'w') as a:
                    a.write(f'{float(0)}\n')
                self._delay = float(0)
                return 0


    def __synthesize(self):
        """
        reads the Verilog file located by self._input_path property and synthesizes it into gate level according to
        tech. library located at "DEFAULT_LIB" and according to the script located at "ABC_SCRIPT_PATH" and dumps the
        synthesized netlist onto "self._syn_path"
        :return: nothing
        """

        yosys_command = f"read_verilog {self._input_path};\n" \
                        f"synth -flatten;\n" \
                        f"opt;\n" \
                        f"opt_clean -purge;\n" \
                        f"abc -liberty {self._lib_path} -script {self._abc_script_path};\n" \
                        f"write_verilog -noattr {self._syn_path}"
        process = subprocess.run([YOSYS, '-p', yosys_command], stdout=PIPE, stderr=PIPE)
        if process.stderr:
            raise Exception(f'Yosys ERROR!!!\n {process.stderr.decode()}')

    def __get_module_name(self):
        """
        reads the Verilog file located at "self._input_path", parses the module signature and extract the module's name
        Example:
        imaging the module signature of a Verilog file is as such:

        module adder_i4_o3(i0, i1, i2, i3, o0, o1, o2);
        ... the rest of the code
        endmodule;

        in this case, this function returns "adder_i4_o3"
        :return: a str that contains the module's name
        """
        with open(self._input_path, 'r') as dp:
            contents = dp.readlines()
            for line in contents:
                if re.search(r'module\s+(.*)\(', line):
                    modulename = re.search(r'module\s+(.*)\(', line).group(1)
        modulename = modulename.strip()
        return modulename

    # =========================
    """
    For use as a PyPI package
    """
    @classmethod
    def area(cls, input_path: str, temp_dir: str = None, report_dir: str = None):
        """
        measures the area with yosys synthesis tool with the tech. library specified
        :return: a float number representing the area
        """
        synth_obj = Synthesis(input_path, temp_dir, report_dir)
        synth_obj._area = synth_obj.get_area()
        return synth_obj._area
    @classmethod
    def power(cls, input_path: str, temp_dir: str = None, report_dir: str = None,
              input_vectors_file: str = None, per_vector_vcd: bool = True,
              distribution=None):
        """
        measures the power with opensta synthesis tool with the tech. library specified
        :return: a float number representing the power
        """
        synth_obj = Synthesis(input_path, temp_dir, report_dir)
        synth_obj._power = synth_obj.get_power(input_vectors_file, per_vector_vcd=per_vector_vcd,
                                               distribution=distribution)
        return synth_obj._power

    @classmethod
    def delay(cls, input_path: str, temp_dir: str = None, report_dir: str = None):
        """
        measures the delay with opensta synthesis tool with the tech. library specified
        :return: a float number representing the delay
        """
        synth_obj = Synthesis(input_path, temp_dir, report_dir)
        synth_obj._delay = synth_obj.get_delay()
        return  synth_obj._delay
    # =========================

    def __repr__(self):
        return f'An object of class Synthesis:\n' \
            f'{self._module_name = }\n' \
            f'{self._input_path = }\n' \
            f'{self._area = }\n' \
            f'{self._power = }\n' \
            f'{self._delay = }\n'


