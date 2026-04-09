import re
import subprocess
from subprocess import PIPE
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

    def get_power(self, input_vectors_file=None):
        """
        measures the power with opensta synthesis tool with the tech. library specified
        If input_vectors_file is provided (path to a file containing input vectors), runs a VCD simulation first.
        """
        self.__synthesize()

        cmds = []
        cmds.append(f"read_liberty {self._lib_path}")
        cmds.append(f"read_verilog {self._syn_path}")
        cmds.append(f"link_design {self._module_name}")
        cmds.append(f"create_clock -name clk -period 1")
        
        # Gestione VCD Simulation
        if input_vectors_file:
            vcd_file = self.__run_vcd_simulation(input_vectors_file)
            if vcd_file and os.path.exists(vcd_file):
                cmds.append(f"read_vcd {vcd_file} -scope tb_{self._module_name}/uut")
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

        with open(f'{self._rep_dir}/{self._module_name}_opensta_debug.log', 'w') as log:
            log.write(f"CMD: {OPENSTA} {self._power_script}\n")
            if process.stdout: log.write(process.stdout.decode())
            if process.stderr: log.write("\nSTDERR:\n" + process.stderr.decode())


        if process.stderr:
            raise Exception(f'OpenSTA ERROR!!!\n {process.stderr.decode()}')
        else:
            if input_vectors_file and os.path.exists(vcd_file):
                os.remove(vcd_file)
            
            os.remove(self._power_script)
            pattern = r"Total\s+(\d+.\d+)[^0-9]*\d+\s+(\d+.\d+)[^0-9]*\d+\s+(\d+.\d+)[^0-9]*\d+\s+(\d+.\d+[^0-9]*\d+)\s+"
            if re.search(pattern, process.stdout.decode()):
                total_power_str = re.search(pattern, process.stdout.decode()).group(4)

                if re.search(r'e[^0-9]*(\d+)', total_power_str):
                    total_power = float(re.search(r'(\d+.\d+)e[^0-9]*\d+', total_power_str).group(1))
                    sign = (re.search(r'e([^0-9]*)(\d+)', total_power_str).group(1))
                    if sign == '-':
                        sign = -1
                    else:
                        sign = +1
                    exponent = int(re.search(r'e([^0-9]*)(\d+)', total_power_str).group(2))
                    total_power = total_power * (10 ** (sign * exponent))
                else:
                    total_power = total_power_str

                with open(f'{self._rep_dir}/{self._module_name}.power', 'w') as a:
                    a.write(f'{float(total_power)}\n')

                self._power = float(total_power)
                return float(total_power)

            else:
                print('OpenSTA Warning! Design has 0 power consumption!')
                with open(f'{self._rep_dir}/{self._module_name}.power', 'w') as a:
                    a.write(f'{float(0)}\n')
                self._power = float(0)
                return 0


    def __format_identifier(self, name: str) -> str:
        """Formats identifiers, adding terminating space for escaped Verilog names."""
        return f"{name} " if name.startswith('\\') else name

    def __natural_sort_key(self, s: str):
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split('([0-9]+)', s)]

    def __canonical_stimulus_inputs(self, inputs):
        """
        Builds canonical bit-level stimulus ordering for input concatenation.
        Output order is group-wise (natural sort by signal base) and MSB->LSB inside each group.
        This keeps bitstream interpretation consistent across vector and scalarized netlists.
        """
        grouped = {}
        scalar_names = []

        for p in inputs:
            decl = p['decl']
            name = p['name']

            # Vector declaration, e.g. "[7:0] a" -> a[7],...,a[0]
            width_match = re.match(r'^\[(\d+)\s*:\s*(\d+)\]\s+(.+)$', decl)
            if width_match and '[' not in name:
                msb = int(width_match.group(1))
                lsb = int(width_match.group(2))
                base = width_match.group(3).strip()
                step = -1 if msb >= lsb else 1
                indices = range(msb, lsb + step, step)
                grouped.setdefault(base, [])
                for idx in indices:
                    grouped[base].append((idx, f"{base}[{idx}]"))
                continue

            # Scalarized indexed port, e.g. "\\A[3]" or "A[3]"
            idx_match = re.match(r'^(\\?[^\[]+)\[(\d+)\]$', name)
            if idx_match:
                base = idx_match.group(1)
                idx = int(idx_match.group(2))
                grouped.setdefault(base, []).append((idx, name))
            else:
                scalar_names.append(name)

        ordered = []
        for base in sorted(grouped.keys(), key=self.__natural_sort_key):
            # Canonical bit ordering is MSB->LSB.
            for _, expr in sorted(grouped[base], key=lambda x: x[0], reverse=True):
                ordered.append(expr)

        ordered.extend(sorted(scalar_names, key=self.__natural_sort_key))
        return ordered

    def __extract_ports(self):
        """
        Parses synthesized Verilog declarations and extracts ports.
        Returns two lists of dictionaries: {'decl': <decl_text>, 'name': <signal_name>}.
        """
        inputs = []
        outputs = []

        with open(self._syn_path, 'r') as f:
            content = f.read()

        for raw_line in content.splitlines():
            line = raw_line.split('//', 1)[0].strip()
            if not line:
                continue

            match = re.match(r'^(input|output)\s+(.+);$', line)
            if not match:
                continue

            direction = match.group(1)
            body = match.group(2).strip()
            body = re.sub(r'^(wire|reg)\s+', '', body)

            width = ''
            width_match = re.match(r'^(\[[^\]]+\])\s+(.*)$', body)
            if width_match:
                width = width_match.group(1)
                body = width_match.group(2).strip()

            names = [p.strip() for p in body.split(',') if p.strip()]
            for name in names:
                decl = f"{width} {name}".strip() if width else name
                entry = {'decl': decl, 'name': name}
                if direction == 'input':
                    inputs.append(entry)
                else:
                    outputs.append(entry)

        return inputs, outputs

    def __run_vcd_simulation(self, input_vectors):
        """
        Generates a testbench, runs iverilog simulation, and returns path to VCD file.
        input_vectors: Path to file only.
                       Each string represents the full concave of A and B bits.
                       Assumes MSB is first char in string.
        """
        inputs, outputs = self.__extract_ports()

        inputs.sort(key=lambda p: self.__natural_sort_key(p['name']))
        stim_inputs = self.__canonical_stimulus_inputs(inputs)

        num_inputs = len(stim_inputs)

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
                tb.write(f"  reg {self.__format_identifier(p['decl'])};\n")
            for p in outputs:
                tb.write(f"  wire {self.__format_identifier(p['decl'])};\n")

            tb.write("  reg clk;\n")

            # Memoria per vettori
            tb.write(f"  reg [{num_inputs}-1:0] test_vectors [0:{num_vectors-1}];\n")
            tb.write("  integer i;\n")

            # Istanza UUT
            tb.write(f"  {self._module_name} uut (\n")
            connections = []
            for p in inputs + outputs:
                port = self.__format_identifier(p['name'])
                sig = self.__format_identifier(p['name'])
                connections.append(f"    .{port}({sig})")
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

            # Input vector mapping is canonicalized bit-wise:
            # test_vectors[i][N-1] -> first element in concat_list,
            # test_vectors[i][0]   -> last element in concat_list.
            # Each indexed signal group is ordered MSB->LSB.
            concat_list = [self.__format_identifier(sig) for sig in stim_inputs]
            if concat_list:
                tb.write(f"      {{ {', '.join(concat_list)} }} = test_vectors[i];\n")

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
    def power(cls, input_path: str, temp_dir: str = None, report_dir: str = None, input_vectors_file: str = None):
        """
        measures the power with opensta synthesis tool with the tech. library specified
        :return: a float number representing the power
        """
        synth_obj = Synthesis(input_path, temp_dir, report_dir)
        synth_obj._power = synth_obj.get_power(input_vectors_file)
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


