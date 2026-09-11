#! /bin/bash
# OSLDMolLib run environment setup (Gaussian 16 + Multiwfn)
#
# Where the values come from: lib.py reads the [GLOBAL] section of config.ini and passes
# each key to this script as an environment variable (see init_environment in lib.py).
# The script can also be run on its own: bash initialize.sh -- it then uses the defaults
# below, or whatever values were exported beforehand.
# Steps that need sudo only print a warning on failure and never abort the run.

# ---- paths (config.ini: g16root / gauss_scrdir) ----
G16ROOT="${g16root:-$HOME}"
export g16root="$G16ROOT"

SCRDIR="${gauss_scrdir:-$G16ROOT/g16_scratch}"
export GAUSS_SCRDIR="$SCRDIR"

# ---- inherit the environment shipped with Gaussian (PATH / GAUSS_EXEDIR / GAUSS_LEXEDIR) ----
PROFILE="${g16_profile:-$G16ROOT/g16/bsd/g16.profile}"
if [ "${source_g16_profile:-1}" = "1" ] && [ -f "$PROFILE" ]; then
    . "$PROFILE"
else
    export GAUSS_EXEDIR="$G16ROOT/g16"
    export PATH="$PATH:$G16ROOT/g16"
    echo "initialize.sh: not sourcing $PROFILE (missing, or source_g16_profile=False), using $G16ROOT/g16" >&2
fi

# ---- OpenMP / memory defaults (config.ini keys of the same name) ----
[ -n "${omp_thread_limit:-}" ] && export OMP_THREAD_LIMIT="${omp_thread_limit}"
[ -n "${omp_stacksize:-}" ] && export OMP_STACKSIZE="${omp_stacksize}"
[ -n "${gauss_memdef:-}" ] && export GAUSS_MEMDEF="${gauss_memdef}"

# ---- Multiwfn (used by the SA calculation) ----
if [ -n "${multiwfn_path:-}" ]; then
    export Multiwfnpath="${multiwfn_path}"
    export PATH="$PATH:${multiwfn_path}"
fi

# ---- stack size ----
if [ "${ulimit_stack_unlimited:-1}" = "1" ]; then
    ulimit -s unlimited 2>/dev/null || echo "initialize.sh: ulimit -s unlimited failed (ignorable)" >&2
fi

# ---- scratch directory ----
mkdir -p "$GAUSS_SCRDIR" 2>/dev/null || echo "initialize.sh: cannot create GAUSS_SCRDIR=$GAUSS_SCRDIR" >&2
# Gaussian must be able to write into its scratch directory; escalate to sudo only if needed
if ! chmod 777 "$GAUSS_SCRDIR" 2>/dev/null; then
    sudo -S chmod 777 "$GAUSS_SCRDIR" \
        || echo "initialize.sh: could not make $GAUSS_SCRDIR writable (ignorable)" >&2
fi

# ---- machine tuning (needs sudo; failures are warnings only) ----
if [ "${tune_cpu:-0}" = "1" ]; then
    sudo -S cpupower frequency-set -u "${cpu_max_freq:-3.7GHz}" >/dev/null \
        || echo "initialize.sh: cpupower frequency-set -u failed (sudo/cpupower required, ignorable)" >&2
    sudo -S cpupower frequency-set -g "${cpu_governor:-performance}" >/dev/null \
        || echo "initialize.sh: cpupower frequency-set -g failed (sudo/cpupower required, ignorable)" >&2
fi

# drop the intermediate variables of this script so lib.py does not pick them up
unset G16ROOT SCRDIR PROFILE

echo "initialize.sh: g16root=$g16root GAUSS_SCRDIR=$GAUSS_SCRDIR Multiwfnpath=${Multiwfnpath:-not set}"
