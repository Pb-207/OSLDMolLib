import configparser
import os
import re
import shlex
import subprocess
import sys
import time
from os import listdir
import multiprocessing
import csv
import matplotlib.pyplot as plt
import numpy as np
import math

# All relative paths (config.ini / Sources / Results / initialize.sh) are resolved against
# the directory containing this file, so `python lib.py` works from any working directory
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

CONFIG_FILE = os.path.join(ROOT, 'config.ini')
INIT_SCRIPT = os.path.join(ROOT, 'initialize.sh')

cf = configparser.ConfigParser()

# [GLOBAL] keys describing the run environment; init_environment() hands them to initialize.sh
ENV_KEYS = (
    'g16root', 'g16_profile', 'source_g16_profile',
    'gauss_scrdir', 'multiwfn_path',
    'omp_thread_limit', 'omp_stacksize', 'gauss_memdef', 'ulimit_stack_unlimited',
    'tune_cpu', 'cpu_max_freq', 'cpu_governor',
)
ENV_PATH_KEYS = ('g16root', 'g16_profile', 'gauss_scrdir', 'multiwfn_path')
# Only these variables are taken back from initialize.sh; every other shell variable is
# ignored so the environment of this process (HOME, TEMP, ...) stays untouched
ENV_ADOPT_PREFIXES = ('GAUSS_', 'OMP_', 'MKL_', 'LD_', 'KMP_')
ENV_ADOPT_NAMES = ('g16root', 'Multiwfnpath', 'PATH')
_ENV_EXPORT_RE = re.compile(r'^declare -x ([A-Za-z_][A-Za-z0-9_]*)=(.*)$', re.M)


def _merge_path(current, new):
    """Add the PATH entries created by the shell, keeping the entries of this process."""
    parts = [p for p in current.split(os.pathsep) if p]
    for p in new.split(os.pathsep):
        if p and p not in parts:
            parts.append(p)
    return os.pathsep.join(parts)


def read_env_config(path=CONFIG_FILE):
    """Read the environment settings from the [GLOBAL] section of config.ini.

    Returns {config key: value} with ~ expanded; empty values are skipped.
    """
    if not os.path.isfile(path):
        print('[env] config not found: ' + path + '; initialize.sh defaults are used')
        return {}

    parser = configparser.ConfigParser()
    parser.read(path, encoding='utf-8')
    if not parser.has_section('GLOBAL'):
        print('[env] ' + path + ' has no [GLOBAL] section; initialize.sh defaults are used')
        return {}

    env = {}
    for key in ENV_KEYS:
        if parser.has_option('GLOBAL', key):
            value = parser.get('GLOBAL', key).strip()
            if value:
                env[key] = os.path.expanduser(value) if key in ENV_PATH_KEYS else value

    missing = [key for key in ('g16root', 'gauss_scrdir') if key not in env]
    if missing:
        print('[env] [GLOBAL] is missing ' + ', '.join(missing)
              + '; initialize.sh defaults are used for them')
    return env


def _parse_export_p(text):
    """Parse the output of `export -p` into {variable name: value}."""
    env = {}
    for name, raw in _ENV_EXPORT_RE.findall(text):
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            raw = re.sub(r'\\(.)', r'\1', raw[1:-1])   # undo bash escaping
        elif raw.startswith("$'"):
            continue        # skip the $'...' form (special characters); defaults are used
        env[name] = raw
    return env


def _apply_env_fallback(env_cfg, only_missing=False):
    """Minimal environment that does not need initialize.sh.

    Sets g16root / GAUSS_SCRDIR / GAUSS_EXEDIR / PATH and the memory and OpenMP defaults.
    With only_missing=True the values already present in the environment are kept.
    """
    g16root = (env_cfg.get('g16root') or os.environ.get('g16root')
               or os.path.expanduser('~'))
    scrdir = (env_cfg.get('gauss_scrdir') or os.environ.get('GAUSS_SCRDIR')
              or os.path.join(g16root, 'g16_scratch'))

    def put(name, value):
        if not only_missing or not os.environ.get(name):
            os.environ[name] = value

    put('g16root', g16root)
    put('GAUSS_SCRDIR', scrdir)
    put('GAUSS_EXEDIR', os.path.join(g16root, 'g16'))
    for name, key in (('OMP_THREAD_LIMIT', 'omp_thread_limit'),
                      ('OMP_STACKSIZE', 'omp_stacksize'),
                      ('GAUSS_MEMDEF', 'gauss_memdef')):
        if env_cfg.get(key):
            put(name, env_cfg[key])

    multiwfn = env_cfg.get('multiwfn_path')
    if multiwfn:
        put('Multiwfnpath', multiwfn)
    for path in (os.environ.get('GAUSS_EXEDIR'), os.environ.get('Multiwfnpath')):
        if path and path not in os.environ.get('PATH', '').split(os.pathsep):
            os.environ['PATH'] = os.environ.get('PATH', '') + os.pathsep + path


def load_global_config(path=CONFIG_FILE):
    """Load the project level config.ini into the global cf as defaults.

    initial_calc() later reads Sources/<molecule>/config.ini, where sections with the same
    name override what was loaded here, so settings of a molecule take precedence. Sections
    that a molecule config does not define fall back to the project values instead of
    raising NoSectionError.
    """
    if os.path.isfile(path):
        cf.read(path, encoding='utf-8')
    else:
        print('[env] config not found: ' + path + '; molecule config.ini files are used')
    return cf


def load_job_config(job_sour):
    """Load the configuration for one molecule: project config.ini first, then the molecule's.

    A fresh parser is created for every job. configparser.read() merges into the existing
    parser and never removes keys, so reusing one parser would leak the settings of the
    previous molecule into the next job (e.g. n_states of a molecule whose config.ini does
    not define that section).
    """
    global cf
    cf = configparser.ConfigParser()
    load_global_config()
    cffile = job_sour + '/config.ini'
    if os.path.isfile(cffile):
        cf.read(cffile, encoding='utf-8')
    return cf


def init_environment():
    """Pass the [GLOBAL] environment settings of config.ini to initialize.sh and take back
    the variables it exports.

    initialize.sh runs in a child shell, so its exports never reach this process. The script
    is therefore sourced and its environment is read back with `export -p` into os.environ,
    which is what makes the g16 / formchk / Multiwfn calls issued by os.system() run with
    the right setup, no matter where lib.py was started from.
    """
    load_global_config()            # project config.ini provides the defaults for each molecule
    env_cfg = read_env_config()
    os.environ.update(env_cfg)      # also readable when initialize.sh runs standalone

    if not os.path.isfile(INIT_SCRIPT):
        print('[env] initialize.sh not found; environment is set from config.ini')
        _apply_env_fallback(env_cfg)
    else:
        # The settings are exported on the command line: environment block names are
        # upper-cased on Windows, which would lose lowercase variables such as g16root
        exports = ' '.join(key + '=' + shlex.quote(value) for key, value in env_cfg.items())
        script = ('set -a; ' + ('export ' + exports + '; ' if exports else '')
                  + '. "$1" >&2; export -p')
        # stderr is not captured, so sudo password prompts and script warnings stay visible
        try:
            result = subprocess.run(['bash', '-c', script, 'initialize.sh', INIT_SCRIPT],
                                    env=dict(os.environ), stdout=subprocess.PIPE, text=True)
        except FileNotFoundError:
            print('[env] bash not available; environment is set from config.ini')
            result = None

        if result is not None:
            if result.returncode != 0:
                print('[env] initialize.sh exited with code ' + str(result.returncode)
                      + ' (continuing)')
            changed = {}
            for name, value in _parse_export_p(result.stdout).items():
                if name == 'PATH':
                    merged = _merge_path(os.environ.get('PATH', ''), value)
                    if merged != os.environ.get('PATH', ''):
                        changed['PATH'] = merged
                elif (name.startswith(ENV_ADOPT_PREFIXES) or name in ENV_ADOPT_NAMES) \
                        and value != os.environ.get(name):
                    changed[name] = value
            os.environ.update(changed)
            print('[env] environment set by initialize.sh: '
                  + (', '.join(sorted(changed)) or 'no change'))
            _apply_env_fallback(env_cfg, only_missing=True)

    print('[env] g16root=' + os.environ.get('g16root', 'unset')
          + '  GAUSS_SCRDIR=' + os.environ.get('GAUSS_SCRDIR', 'unset')
          + '  n_para=' + str(cf.getint('GLOBAL', 'n_para') if cf.has_section('GLOBAL') else '?'))


def get_mol_lst():
    mol = []
    with open('Sources/job.lst', 'w') as mol_lst_file:
        for name in sorted(listdir('Sources')):
            if name.startswith('.') or not os.path.isdir(os.path.join('Sources', name)):
                continue
            mol.append(name)
            mol_lst_file.write(name + '\n')
        mol_lst_file.close()

    return mol


def gen_gjf(gjf_path, n_cores, max_ram, chk, options, title, charge, spin, coor):
    with open(gjf_path, 'w') as gjf_file:
        gjf_file.write('%mem=' + max_ram + 'GB\n%nprocshared=' + n_cores + '\n%chk=' + chk + '\n' + options + '\n\n' + title + '\n\n' + charge + ' ' + spin + '\n' + coor + '\n')
        gjf_file.close()


def initial_lib():
    init_environment()
    for directory in ('Sources', 'Results', 'Calculated'):
        os.makedirs(directory, exist_ok=True)      # git does not track empty directories
    if not os.path.isfile('Results/mol_lst.csv'):
        with open('Results/mol_lst.csv', 'w') as mol_lst:
            mol_lst.write('Molecule,Emission Wavelength (nm),Oscillator Strength\n')
            mol_lst.close()


class DuplicateJobError(Exception):
    """A queued molecule has the same name as one that is already archived in Calculated/.

    The pipeline does not rename it automatically: two directories that only differ by a
    '-1' suffix are hard to tell apart later, and if the rename target already exists mv
    nests the directories instead of renaming them. Resolve the duplicate by hand.
    """

    def __init__(self, job):
        self.job = job
        super().__init__(
            "job '" + job + "' already exists in Calculated/ (it has been calculated).\n"
            "Rename the queued directory Sources/" + job
            + " or remove the archived directory Calculated/" + job
            + ", then start the run again.")


def initial_calc():
    global current_job, current_job_path, ori_coor, sum_file_path, current_job_file
    job_lst = get_mol_lst()
    while len(job_lst) < 1:
        print('All jobs are finished, input new job.')
        time.sleep(60)
        job_lst = get_mol_lst()

    current_job = job_lst[0]
    if os.path.isdir('Calculated/' + current_job):
        raise DuplicateJobError(current_job)

    current_job_path = 'Results/' + current_job
    print('\n', 'Remaining jobs:', len(job_lst))
    os.makedirs(current_job_path, exist_ok=True)

    current_job_sour = 'Sources/' + current_job
    for file in listdir(current_job_sour):
        if '.gjf' in file:
            current_job_file = current_job_sour + '/' + file

    with open(current_job_file, 'r') as ori_gjf:
        lines = ori_gjf.readlines()
        ori_gjf.close()

    ori_coor = ''
    for line in lines:
        if line[0] == " ":
            ori_coor = ori_coor + line

    load_job_config(current_job_sour)

    options = '#p opt ' + cf.get('S0OPT', 'method') + '/' + cf.get('S0OPT', 'basis_set')
    gjf_path = 'Results/' + current_job + '/' + current_job + '_S0opt.gjf'
    n_cores = cf.get('GLOBAL', 'n_cores')
    max_ram = cf.get('GLOBAL', 'max_ram')
    chk = current_job + '_S0opt.chk'
    title = 'S0OPT'
    charge = '0'
    spin = '1'
    gen_gjf(gjf_path, n_cores, max_ram, chk, options, title, charge, spin, str(ori_coor))
    sum_file_path = current_job_path + '/sum.csv'
    with open(sum_file_path, 'w') as sum_file:
        sum_file.write('Geometry,State,Energy (eV),Wavelength (nm),Oscillator Strength\n')
        sum_file.close()


class CalcError(Exception):
    """A Gaussian job did not finish normally.

    The .out file is kept where it is: it holds the Gaussian log, which is what you need to
    find out why the job failed.
    """

    def __init__(self, step, out_file_path):
        self.step = step
        self.out_file_path = out_file_path
        super().__init__('Calculation error in ' + step + ' (' + out_file_path + ')')


def run_g16(path, gjf):
    run = True
    out_file_path = path + '/' + gjf + '.out'
    if os.path.isfile(out_file_path):
        with open(out_file_path, 'r') as ori_gjf:
            lines = ori_gjf.readlines()
            ori_gjf.close()

        if lines and 'Normal termination' in lines[-1]:
            run = False

    if run:
        cmd = 'cd ' + path + ' && g16 < ' + gjf + '.gjf > ' + gjf + '.out'
        print(cmd)
        os.system(cmd)

    if not os.path.isfile(out_file_path):
        raise CalcError(gjf, out_file_path)       # g16 did not even produce an output file

    with open(out_file_path, 'r') as ori_gjf:
        lines = ori_gjf.readlines()
        ori_gjf.close()

    if not lines or 'Normal termination' not in lines[-1]:
        raise CalcError(gjf, out_file_path)


def run_S0opt():
    gjf = current_job + '_S0opt'
    run_g16(current_job_path, gjf)


def gen_opt_gjf():
    if cf.getboolean('S0ABS', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p td=(nstates=' + cf.get('S0ABS', 'n_states') + ') ' + cf.get('S0ABS', 'method') + '/' + cf.get('S0ABS', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_S0abs.chk'
        title = 'S0ABS'
        charge = '0'
        spin = '1'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_S0opt.out'
        gjf_file = current_job_path + '/' + current_job + '_S0abs.gjf'
        out2gjf(out_file, gjf_file)

    if cf.getboolean('S1OPT', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p opt td=(nstates=' + cf.get('S1OPT', 'n_states') + ') ' + cf.get('S1OPT', 'method') + '/' + cf.get('S1OPT', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_S1opt.chk'
        title = 'S1OPT'
        charge = '0'
        spin = '1'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_S0opt.out'
        gjf_file = current_job_path + '/' + current_job + '_S1opt.gjf'
        out2gjf(out_file, gjf_file)

    if cf.getboolean('T1OPT', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p opt ' + cf.get('T1OPT', 'method') + '/' + cf.get('T1OPT', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_T1opt.chk'
        title = 'T1OPT'
        charge = '0'
        spin = '3'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_S0opt.out'
        gjf_file = current_job_path + '/' + current_job + '_T1opt.gjf'
        out2gjf(out_file, gjf_file)

    if cf.getboolean('E0OPT', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p opt ' + cf.get('E0OPT', 'method') + '/' + cf.get('E0OPT', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_E0opt.chk'
        title = 'E0OPT'
        charge = '-1'
        spin = '2'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_S0opt.out'
        gjf_file = current_job_path + '/' + current_job + '_E0opt.gjf'
        out2gjf(out_file, gjf_file)

    if cf.getboolean('H0OPT', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p opt ' + cf.get('H0OPT', 'method') + '/' + cf.get('H0OPT', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_H0opt.chk'
        title = 'H0OPT'
        charge = '1'
        spin = '2'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_S0opt.out'
        gjf_file = current_job_path + '/' + current_job + '_H0opt.gjf'
        out2gjf(out_file, gjf_file)


def out2gjf(out_file, gjf_file):
    skip_file = gjf_file.replace('.gjf', '.out')
    if not os.path.isfile(skip_file):
        cmd = './out2gjf.sh ' + current_job_path + '/settings.gjf ' + out_file
        os.system(cmd)
        cmd = 'mv temp.gjf ' + gjf_file
        os.system(cmd)


def run_S0abs():
    if cf.getboolean('S0ABS', 'enable'):
        gjf = current_job + '_S0abs'
        run_g16(current_job_path, gjf)
        out_file_path = current_job_path + '/' + gjf + '.out'
        with open(out_file_path, 'r') as out_file:
            lines = out_file.readlines()
            out_file.close()

        with open(sum_file_path, 'a') as sum_file:
            for line in lines:
                if 'Excited State' in line:
                    line = line.replace(":", "").replace("f=", "")
                    line = line.split()
                    content = 'S0,0->' + line[2] + ',' + line[4] + ',' + line[6] + ',' + line[8] + '\n'
                    sum_file.write(content)
            sum_file.close()


def run_S1opt():
    if cf.getboolean('S1OPT', 'enable'):
        gjf = current_job + '_S1opt'
        run_g16(current_job_path, gjf)


def run_S1abs():
    if cf.getboolean('S1OPT', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p td=(nstates=' + cf.get('S1ABS', 'n_states') + ') ' + cf.get('S1ABS', 'method') + '/' + cf.get('S1ABS', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_S1abs.chk'
        title = 'S1ABS'
        charge = '0'
        spin = '1'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_S1opt.out'
        gjf_file = current_job_path + '/' + current_job + '_S1abs.gjf'
        out2gjf(out_file, gjf_file)
        gjf = current_job + '_S1abs'
        run_g16(current_job_path, gjf)
        out_file_path = current_job_path + '/' + gjf + '.out'
        with open(out_file_path, 'r') as out_file:
            lines = out_file.readlines()
            out_file.close()

        with open(sum_file_path, 'a') as sum_file:
            for line in lines:
                if 'Excited State' in line:
                    line = line.replace(":", "").replace("f=", "")
                    line = line.split()
                    content = 'S1,0->' + line[2] + ',' + line[4] + ',' + line[6] + ',' + line[8] + '\n'
                    sum_file.write(content)
            sum_file.close()


def run_T1opt():
    if cf.getboolean('T1OPT', 'enable'):
        gjf = current_job + '_T1opt'
        run_g16(current_job_path, gjf)


def run_T1abs():
    if cf.getboolean('T1OPT', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p td=(nstates=' + cf.get('T1ABS', 'n_states') + ') ' + cf.get('T1ABS', 'method') + '/' + cf.get('T1ABS', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_T1abs.chk'
        title = 'T1ABS'
        charge = '0'
        spin = '3'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_T1opt.out'
        gjf_file = current_job_path + '/' + current_job + '_T1abs.gjf'
        out2gjf(out_file, gjf_file)
        gjf = current_job + '_T1abs'
        run_g16(current_job_path, gjf)
        out_file_path = current_job_path + '/' + gjf + '.out'
        with open(out_file_path, 'r') as out_file:
            lines = out_file.readlines()
            out_file.close()

        with open(sum_file_path, 'a') as sum_file:
            for line in lines:
                if 'Excited State' in line:
                    line = line.replace(":", "").replace("f=", "")
                    line = line.split()
                    content = 'T1,1->' + str(int(line[2]) + 1) + ',' + line[4] + ',' + line[6] + ',' + line[8] + '\n'
                    sum_file.write(content)
            sum_file.close()


def run_E0opt():
    if cf.getboolean('E0OPT', 'enable'):
        gjf = current_job + '_E0opt'
        run_g16(current_job_path, gjf)


def run_E0abs():
    if cf.getboolean('E0OPT', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p td=(nstates=' + cf.get('E0ABS', 'n_states') + ') ' + cf.get('E0ABS', 'method') + '/' + cf.get('E0ABS', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_E0abs.chk'
        title = 'E0ABS'
        charge = '-1'
        spin = '2'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_E0opt.out'
        gjf_file = current_job_path + '/' + current_job + '_E0abs.gjf'
        out2gjf(out_file, gjf_file)
        gjf = current_job + '_E0abs'
        run_g16(current_job_path, gjf)
        out_file_path = current_job_path + '/' + gjf + '.out'
        with open(out_file_path, 'r') as out_file:
            lines = out_file.readlines()
            out_file.close()

        with open(sum_file_path, 'a') as sum_file:
            for line in lines:
                if 'Excited State' in line:
                    line = line.replace(":", "").replace("f=", "")
                    line = line.split()
                    content = 'E0,0->' + line[2] + ',' + line[4] + ',' + line[6] + ',' + line[8] + '\n'
                    sum_file.write(content)
            sum_file.close()


def run_H0opt():
    if cf.getboolean('H0OPT', 'enable'):
        gjf = current_job + '_H0opt'
        run_g16(current_job_path, gjf)


def run_H0abs():
    if cf.getboolean('H0OPT', 'enable'):
        settings_path = current_job_path + '/settings.gjf'
        options = '#p td=(nstates=' + cf.get('H0ABS', 'n_states') + ') ' + cf.get('H0ABS', 'method') + '/' + cf.get('H0ABS', 'basis_set')
        n_cores = str(cf.getint('GLOBAL', 'n_cores') // cf.getint('GLOBAL', 'n_para'))
        max_ram = str(cf.getint('GLOBAL', 'max_ram') // cf.getint('GLOBAL', 'n_para'))
        chk = current_job + '_H0abs.chk'
        title = 'H0ABS'
        charge = '1'
        spin = '2'
        gen_gjf(settings_path, n_cores, max_ram, chk, options, title, charge, spin, ori_coor)
        out_file = current_job_path + '/' + current_job + '_H0opt.out'
        gjf_file = current_job_path + '/' + current_job + '_H0abs.gjf'
        out2gjf(out_file, gjf_file)
        gjf = current_job + '_H0abs'
        run_g16(current_job_path, gjf)
        out_file_path = current_job_path + '/' + gjf + '.out'
        with open(out_file_path, 'r') as out_file:
            lines = out_file.readlines()
            out_file.close()

        with open(sum_file_path, 'a') as sum_file:
            for line in lines:
                if 'Excited State' in line:
                    line = line.replace(":", "").replace("f=", "")
                    line = line.split()
                    content = 'H0,0->' + line[2] + ',' + line[4] + ',' + line[6] + ',' + line[8] + '\n'
                    sum_file.write(content)
            sum_file.close()


def job(num):
    if num == 0:
        run_S0abs()
    elif num == 1:
        run_S1opt()
        run_S1abs()
    elif num == 2:
        run_T1opt()
        run_T1abs()
    elif num == 3:
        run_E0opt()
        run_E0abs()
    elif num == 4:
        run_H0opt()
        run_H0abs()


def calc_cur(data, sig):
    x = np.arange(min_wavelen, max_wavelen, wavelen_step)
    y = float(0)
    for stat in data:
        add = float(stat[1]) * np.exp(- (x - float(stat[0])) ** 2 / (2 * sig ** 2))
        y = np.add(y, add)

    return x, y


def gen_graph():
    global min_wavelen, max_wavelen, wavelen_step, sig
    csv_path = current_job_path + '/sum.csv'
    with open(csv_path, 'r') as csv_file:
        csv_data = list(csv.reader(csv_file))
        csv_file.close()

    abs_data = [[str(0), str(0)]]
    em_data = []
    sa_data = [[str(0), str(0)]]
    ta_data = [[str(0), str(0)]]
    ea_data = [[str(0), str(0)]]
    ha_data = [[str(0), str(0)]]
    for data in csv_data:
        if data[0] == 'S0':
            add = [data[3], data[4]]
            abs_data.append(add)
        elif data[0] == 'S1' and data[1] == '0->1':
            add = [data[3], data[4]]
            em_data.append(add)
        elif data[0] == 'T1':
            add = [data[3], data[4]]
            ta_data.append(add)
        elif data[0] == 'E0':
            add = [data[3], data[4]]
            ea_data.append(add)
        elif data[0] == 'H0':
            add = [data[3], data[4]]
            ha_data.append(add)

    csv_path = current_job_path + '/transdipmom.txt'
    with open(csv_path, 'r') as csv_file:
        csv_data = csv_file.readlines()
        csv_file.close()

    with open(sum_file_path, 'a') as sum_file:
        for line in csv_data:
            data = line.split()
            if len(data) > 6:
                if data[0] == '1' and data[1] != '1':
                    wavelen = 1239.8 / float(data[5])
                    add = [wavelen, data[6]]
                    sa_data.append(add)
                    content = 'S1,1->' + data[1] + ',' + data[5] + ',' + str(wavelen) + ',' + data[6] + '\n'
                    sum_file.write(content)
        sum_file.close()

    min_wavelen = cf.getfloat('GRAPH', 'min_wavelen')
    max_wavelen = cf.getfloat('GRAPH', 'max_wavelen')
    wavelen_step = cf.getfloat('GRAPH', 'wavelen_step')
    sig = cf.getfloat('GRAPH', 'fwhm') / 2 / np.sqrt(2 * np.log(2))
    x, y_abs = calc_cur(abs_data, sig)
    x, y_em = calc_cur(em_data, sig)
    x, y_sa = calc_cur(sa_data, sig)
    x, y_ta = calc_cur(ta_data, sig)
    x, y_ea = calc_cur(ea_data, sig)
    x, y_ha = calc_cur(ha_data, sig)
    if os.path.isfile('Results/mol_lst.csv'):
        with open('Results/mol_lst.csv', 'a') as mol_lst:
            content = current_job + ',' + em_data[0][0] + ',' + em_data[0][1] + '\n'
            mol_lst.write(content)
            mol_lst.close()

    plt.plot(x, y_sa, color=cf.get('GRAPH', 'sa_color'))
    plt.plot(x, y_ha, color=cf.get('GRAPH', 'ha_color'))
    plt.plot(x, y_ea, color=cf.get('GRAPH', 'ea_color'))
    plt.plot(x, y_ta, color=cf.get('GRAPH', 'ta_color'))
    plt.plot(x, y_abs, color=cf.get('GRAPH', 'abs_color'))
    plt.plot(x, y_em, color=cf.get('GRAPH', 'em_color'))
    plt.legend(labels=['SA', 'P$^+$A', 'P$^-$A', 'TA', 'Abs', 'Emit'])
    plt.title(current_job)
    plt.xlabel('Wavelength (nm)')
    plt.ylabel('Oscillator Strength')
    pdf_path = current_job_path + '/sum.pdf'
    plt.savefig(pdf_path, dpi=cf.getint('GRAPH', 'dpi'), format='pdf')
    plt.close()


def ana_sa():
    chk_file_path = current_job_path + '/' + current_job + '_S1abs.chk'
    cmd = 'formchk ' + chk_file_path
    os.system(cmd)
    chk_file_path = current_job + '_S1abs.fchk'
    cmd = 'cd ' + current_job_path + ' && Multiwfn ' + chk_file_path + ' < ../../multiwfn.cmd > /dev/null 2>&1 /dev/null'
    os.system(cmd)


def collect_failures(results):
    """Take back the exceptions raised inside the pool workers.

    apply_async() stores the exception in its AsyncResult and never raises it, so without
    this step a worker that fails would silently disappear together with its results.
    """
    failures = []
    for result in results:
        if result.successful():
            continue
        try:
            result.get()
        except Exception as error:
            failures.append(error)
    return failures


def archive_failed_job(failures):
    """Handle a molecule that did not finish.

    Keeps the files already written to Results/<job> (the .out files hold the Gaussian log,
    which is what shows why the job failed), marks the molecule as failed in the summary
    tables and archives the source directory to Calculated/. The caller then continues with
    the next molecule instead of stopping the whole run.
    """
    steps = []
    for error in failures:
        step = getattr(error, 'step', type(error).__name__)
        if step.startswith(current_job + '_'):
            step = step[len(current_job) + 1:]
        steps.append(step)
        print('[error]', step, '->', error)

    if os.path.isfile(sum_file_path):
        with open(sum_file_path, 'a') as sum_file:
            for step in steps:
                sum_file.write(step + ',error,,,\n')

    with open('Results/mol_lst.csv', 'a') as mol_lst:
        mol_lst.write(current_job + ',error,error\n')

    try:
        os.rename('Sources/' + current_job, 'Calculated/' + current_job)
        print('[error]', current_job, 'archived to Calculated/, continuing with the next job')
    except OSError as error:
        print('[error] could not archive', current_job, '->', error)


def run_job():
    while True:
        initial_calc()
        failures = []
        try:
            run_S0opt()
            gen_opt_gjf()
            # The workers are created by fork and inherit the module globals set by
            # initial_calc() (current_job, current_job_path, sum_file_path, ori_coor, cf) --
            # that is why job() takes no arguments. Linux only: with the spawn start method
            # (macOS, Windows) these names are undefined inside the workers.
            pool = multiprocessing.Pool(processes=cf.getint('GLOBAL', 'n_para'))
            results = [pool.apply_async(job, (i,)) for i in range(5)]
            pool.close()
            pool.join()
            failures = collect_failures(results)
        except Exception as error:
            failures = [error]

        if failures:
            archive_failed_job(failures)
            continue

        ana_sa()
        gen_graph()
        cmd = 'mv Sources/' + current_job + ' Calculated/'
        os.system(cmd)


if __name__ == '__main__':
    initial_lib()
    try:
        run_job()
    except DuplicateJobError as error:
        print('[' + type(error).__name__ + '] ' + str(error), file=sys.stderr)
        sys.exit(1)