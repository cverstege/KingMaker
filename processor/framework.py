import os
import luigi
import law
import select
from law.util import interruptable_popen, readable_popen
from subprocess import PIPE, Popen
from rich.console import Console
from law.util import merge_dicts, DotDict
from datetime import datetime
from law.contrib.htcondor.job import HTCondorJobManager
from tempfile import mkdtemp
from getpass import getuser
from law.target.collection import flatten_collections
from law.config import Config

law.contrib.load("wlcg")
law.contrib.load("htcondor")
# try to get the terminal width, if this fails, we are in a remote job, set it to 140
try:
    current_width = os.get_terminal_size().columns
except OSError:
    current_width = 140
console = Console(width=current_width)

# Determine startup time to use as default production_tag
# LOCAL_TIMESTAMP is used by remote workflows to ensure consistent tags
if os.getenv("LOCAL_TIMESTAMP"):
    startup_time = os.getenv("LOCAL_TIMESTAMP")
else:
    startup_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f")

# Determine start dir to replace absolute paths
# LOCAL_PWD is used by remote workflows
if os.getenv("LOCAL_PWD"):
    startup_dir = os.getenv("LOCAL_PWD")
else:
    startup_dir = os.getcwd()


class Task(law.Task):
    local_user = getuser()
    wlcg_path = luigi.Parameter(description="Base-path to remote file location.")
    # Behaviour of production_tag:
    # If a tag is give it will be used for the respective task.
    # If no tag is given a timestamp abse on startup_time is used.
    #   This timestamp is the same for all tasks with no set production_tag.
    production_tag = luigi.Parameter(
        default="default/{}".format(startup_time),
        description="Tag to differentiate workflow runs. Set to a timestamp as default.",
    )
    output_collection_cls = law.NestedSiblingFileCollection

    # Path of local targets. Composed from the analysis path set during the setup.sh,
    #   the production_tag, the name of the task and an additional path if provided.
    def local_path(self, *path):
        parts = (
            (os.getenv("ANALYSIS_DATA_PATH"),)
            + (self.production_tag,)
            + (self.__class__.__name__,)
            + path
        )
        return os.path.join(*parts)

    def temporary_local_path(self, *path):
        if os.environ.get("_CONDOR_JOB_IWD"):
            prefix = os.environ.get("_CONDOR_JOB_IWD") + "/tmp/"
        else:
            prefix = "/tmp/{user}".format(user=self.local_user)
        temporary_dir = mkdtemp(dir=prefix)
        parts = (temporary_dir,) + (self.__class__.__name__,) + path
        return os.path.join(*parts)

    def local_target(self, path):
        return law.LocalFileTarget(self.local_path(path))

    def local_targets(self, paths):
        targets = []
        for path in paths:
            targets.append(law.LocalFileTarget(path=self.local_path(path)))
        return targets

    def temporary_local_target(self, *path):
        return law.LocalFileTarget(self.temporary_local_path(*path))

    # Path of remote targets. Composed from the production_tag,
    #   the name of the task and an additional path if provided.
    #   The wlcg_path will be prepended for WLCGFileTargets
    def remote_path(self, *path):
        parts = (self.production_tag,) + (self.__class__.__name__,) + path
        return os.path.join(*parts)

    def remote_target(self, path):
        target = law.wlcg.WLCGFileTarget(path=self.remote_path(path))
        return target

    def remote_targets(self, paths):
        targets = []
        for path in paths:
            targets.append(law.wlcg.WLCGFileTarget(path=self.remote_path(path)))
        return targets

    def convert_env_to_dict(self, env):
        my_env = {}
        for line in env.splitlines():
            if line.find(" ") < 0:
                try:
                    key, value = line.split("=", 1)
                    my_env[key] = value
                except ValueError:
                    pass
        return my_env

    # Function to apply a source-script and get the resulting environment.
    #   Anything apart from setting paths is likely not included in the resulting envs.
    def set_environment(self, sourcescript, silent=False):
        if not silent:
            console.log("with source script: {}".format(sourcescript))
        if isinstance(sourcescript, str):
            sourcescript = [sourcescript]
        source_command = [
            "source {};".format(sourcescript) for sourcescript in sourcescript
        ] + ["env"]
        source_command_string = " ".join(source_command)
        code, out, error = interruptable_popen(
            source_command_string,
            shell=True,
            stdout=PIPE,
            stderr=PIPE,
            # rich_console=console
        )
        if code != 0:
            console.log("source returned non-zero exit status {}".format(code))
            console.log("Error: {}".format(error))
            raise Exception("source failed")
        my_env = self.convert_env_to_dict(out)
        return my_env

    # Run a bash command
    #   Command can be composed of multiple parts (interpreted as seperated by a space).
    #   A sourcescript can be provided that is called by set_environment the resulting
    #       env is then used for the command
    #   The command is run as if it was called from run_location
    #   With "collect_out" the output of the run command is returned
    def run_command(
        self,
        command=[],
        sourcescript=[],
        run_location=None,
        collect_out=False,
        silent=False,
    ):
        if command:
            if isinstance(command, str):
                command = [command]
            logstring = "Running {}".format(command)
            if run_location:
                logstring += " from {}".format(run_location)
            if not silent:
                console.log(logstring)
            if sourcescript:
                run_env = self.set_environment(sourcescript, silent)
            else:
                run_env = None
            if not silent:
                console.rule()
            code, out, error = interruptable_popen(
                " ".join(command),
                shell=True,
                stdout=PIPE,
                stderr=PIPE,
                env=run_env,
                cwd=run_location,
            )
            if not silent:
                console.log("Output: {}".format(out))
                console.rule()
            if not silent or code != 0:
                console.log("Error: {}".format(error))
                console.rule()
            if code != 0:
                console.log("Error when running {}.".format(list(command)))
                console.log("Command returned non-zero exit status {}.".format(code))
                raise Exception("{} failed".format(list(command)))
            else:
                if not silent:
                    console.log("Command successful.")
            if collect_out:
                return out
        else:
            raise Exception("No command provided.")

    def run_command_readable(self, command=[], sourcescript=[], run_location=None):
        """
        This can be used, to run a command, where you want to read the output while the command is running.
        redirect both stdout and stderr to the same output.
        """
        if command:
            if isinstance(command, str):
                command = [command]
            if sourcescript:
                run_env = self.set_environment(sourcescript)
            else:
                run_env = None
            logstring = "Running {}".format(command)
            if run_location:
                logstring += " from {}".format(run_location)
            console.rule()
            console.log(logstring)
            try:
                p = Popen(
                    " ".join(command),
                    shell=True,
                    stdout=PIPE,
                    stderr=PIPE,
                    env=run_env,
                    cwd=run_location,
                    encoding="utf-8",
                )
                while True:
                    reads = [p.stdout.fileno(), p.stderr.fileno()]
                    ret = select.select(reads, [], [])

                    for fd in ret[0]:
                        if fd == p.stdout.fileno():
                            read = p.stdout.readline()
                            if read != "\n":
                                console.log(read.strip())
                        if fd == p.stderr.fileno():
                            read = p.stderr.readline()
                            if read != "\n":
                                console.log(read.strip())

                    if p.poll() != None:
                        break
                if p.returncode != 0:
                    raise Exception(f"Error when running {command}.")
            except Exception as e:
                raise Exception(f"Error when running {command}.")
        else:
            raise Exception("No command provided.")


class HTCondorWorkflow(Task, law.htcondor.HTCondorWorkflow):
    ENV_NAME = luigi.Parameter(description="Environment to be used in HTCondor job.")
    htcondor_accounting_group = luigi.Parameter(
        description="Accounting group to be set in Hthe TCondor job submission."
    )
    htcondor_requirements = luigi.Parameter(
        default="",
        description="Job requirements to be set in the HTCondor job submission.",
    )
    htcondor_remote_job = luigi.Parameter(
        description="Whether RemoteJob should be set in the HTCondor job submission."
    )
    htcondor_walltime = luigi.Parameter(
        description="Runtime to be set in HTCondor job submission."
    )
    htcondor_request_cpus = luigi.Parameter(
        description="Number of CPU cores to be requested in HTCondor job submission."
    )
    htcondor_request_gpus = luigi.Parameter(
        default="0",
        description="Number of GPUs to be requested in HTCondor job submission. Default is none.",
    )
    htcondor_request_memory = luigi.Parameter(
        description="Amount of memory(MB) to be requested in HTCondor job submission."
    )
    htcondor_universe = luigi.Parameter(
        description="Universe to be set in HTCondor job submission."
    )
    htcondor_docker_image = luigi.Parameter(
        description="Docker image to be used in HTCondor job submission."
    )
    htcondor_request_disk = luigi.Parameter(
        description="Amount of scratch-space(kB) to be requested in HTCondor job submission."
    )
    bootstrap_file = luigi.Parameter(
        description="Bootstrap script to be used in HTCondor job to set up law."
    )
    additional_files = luigi.ListParameter(
        default=[],
        description="Additional files to be included in the job tarball. Will be unpacked in the run directory",
    )

    # Use proxy file located in $X509_USER_PROXY or /tmp/x509up_u$(id) if empty
    htcondor_user_proxy = law.wlcg.get_voms_proxy_file()

    def htcondor_create_job_manager(self, **kwargs):
        kwargs = merge_dicts(self.htcondor_job_manager_defaults, kwargs)
        return HTCondorJobManager(**kwargs)

    def htcondor_output_directory(self):
        # Add identification-str to prevent interference between different tasks of the same class
        # Expand path to account for use of env variables (like $USER)
        return law.wlcg.WLCGDirectoryTarget(
            self.remote_path("htcondor_files", self.task_id),
            law.wlcg.WLCGFileSystem(
                None, base="{}".format(os.path.expandvars(self.wlcg_path))
            ),
        )

    def htcondor_create_job_file_factory(self):
        factory = super(HTCondorWorkflow, self).htcondor_create_job_file_factory()
        factory.is_tmp = False
        # Print location of job dir
        console.log("HTCondor job directory is: {}".format(factory.dir))
        return factory

    def htcondor_bootstrap_file(self):
        hostfile = self.bootstrap_file
        return law.util.rel_path(__file__, hostfile)

    def htcondor_job_config(self, config, job_num, branches):
        analysis_name = os.getenv("ANA_NAME")
        task_name = self.__class__.__name__
        _cfg = Config.instance()
        job_file_dir = _cfg.get_expanded("job", "job_file_dir")
        logdir = os.path.join(
            os.path.dirname(job_file_dir), "logs", self.production_tag
        )
        for file_ in ["Log", "Output", "Error"]:
            os.makedirs(os.path.join(logdir, file_), exist_ok=True)
        logfile = os.path.join(
            logdir, "Log", "{}_{}to{}.txt".format(task_name, branches[0], branches[-1])
        )
        outfile = os.path.join(
            logdir,
            "Output",
            "{}_{}to{}.txt".format(task_name, branches[0], branches[-1]),
        )
        errfile = os.path.join(
            logdir,
            "Error",
            "{}_{}to{}.txt".format(task_name, branches[0], branches[-1]),
        )

        # Write job config file
        config.custom_content = []
        config.custom_content.append(
            ("accounting_group", self.htcondor_accounting_group)
        )
        config.custom_content.append(("Log", logfile))
        config.custom_content.append(("Output", outfile))
        config.custom_content.append(("Error", errfile))

        config.custom_content.append(("stream_error", "True"))  # Remove before commit
        config.custom_content.append(("stream_output", "True"))  #
        if self.htcondor_requirements:
            config.custom_content.append(("Requirements", self.htcondor_requirements))
        config.custom_content.append(("+RemoteJob", self.htcondor_remote_job))
        config.custom_content.append(("universe", self.htcondor_universe))
        config.custom_content.append(("docker_image", self.htcondor_docker_image))
        config.custom_content.append(("+RequestWalltime", self.htcondor_walltime))
        config.custom_content.append(("x509userproxy", self.htcondor_user_proxy))
        config.custom_content.append(("request_cpus", self.htcondor_request_cpus))
        # Only include "request_gpus" if any are requested, as nodes with GPU are otherwise excluded
        if float(self.htcondor_request_gpus) > 0:
            config.custom_content.append(("request_gpus", self.htcondor_request_gpus))
        config.custom_content.append(("RequestMemory", self.htcondor_request_memory))
        config.custom_content.append(("RequestDisk", self.htcondor_request_disk))

        # Ensure tarball dir exists
        if not os.path.exists("tarballs/{}".format(self.production_tag)):
            os.makedirs("tarballs/{}".format(self.production_tag))
        # Repack tarball if it is not available remotely
        tarball = law.wlcg.WLCGFileTarget(
            "{tag}/{task}/job_tarball/processor.tar.gz".format(
                tag=self.production_tag, task=self.__class__.__name__
            )
        )
        if not tarball.exists():
            # Make new tarball
            prevdir = os.getcwd()
            os.system("cd $ANALYSIS_PATH")
            # get absolute path to tarball dir
            tarball_dir = os.path.abspath("tarballs/{}".format(self.production_tag))
            tarball_local = law.LocalFileTarget(
                "{}/{}/processor.tar.gz".format(tarball_dir, task_name)
            )
            print(
                f"Uploading framework tarball from {tarball_local.path} to {tarball.path}"
            )
            tarball_local.parent.touch()
            # Create tarball containing:
            #   The processor directory, thhe relevant config files, law
            #   and any other files specified in the additional_files parameter
            command = [
                "tar",
                "--exclude",
                "*.pyc",
                "--exclude",
                "*.git",
                "-czf",
                "{}/{}/processor.tar.gz".format(tarball_dir, task_name),
                "processor",
                "lawluigi_configs/{}_luigi.cfg".format(analysis_name),
                "lawluigi_configs/{}_law.cfg".format(analysis_name),
                "law",
            ] + list(self.additional_files)
            code, out, error = interruptable_popen(
                command,
                stdout=PIPE,
                stderr=PIPE,
                # rich_console=console
            )
            if code != 0:
                console.log("Error when taring job {}".format(error))
                console.log("Output: {}".format(out))
                console.log("tar returned non-zero exit status {}".format(code))
                console.rule()
                os.remove("/{}/processor.tar.gz".format(tarball_dir, task_name))
                raise Exception("tar failed")
            else:
                console.rule("Successful tar of framework tarball !")
            # Copy new tarball to remote
            tarball.parent.touch()
            tarball.copy_from_local(
                src="{}/{}/processor.tar.gz".format(tarball_dir, task_name)
            )
            console.rule("Framework tarball uploaded!")
            os.chdir(prevdir)
        # Check if env of this task was found in cvmfs
        env_list = os.getenv("ENV_NAMES_LIST").split(";")
        env_list = list(dict.fromkeys(env_list[:-1]))
        env_dict = dict(env.split(",") for env in env_list)
        if env_dict[self.ENV_NAME] == "False":
            # IMPORTANT: environments have to be named differently with each change
            #            as caching prevents a clean overwrite of existing files
            tarball_env = law.wlcg.WLCGFileTarget(
                path="env_tarballs/{env}.tar.gz".format(env=self.ENV_NAME)
            )
            if not tarball_env.exists():
                tarball_env.parent.touch()
                tarball_env.copy_from_local(
                    src="tarballs/conda_envs/{}.tar.gz".format(self.ENV_NAME)
                )
        config.render_variables["USER"] = self.local_user
        config.render_variables["ANA_NAME"] = os.getenv("ANA_NAME")
        config.render_variables["ENV_NAME"] = self.ENV_NAME
        config.render_variables["TAG"] = self.production_tag
        config.render_variables["USE_CVMFS"] = env_dict[self.ENV_NAME]
        config.render_variables["LUIGIPORT"] = os.getenv("LUIGIPORT")
        config.render_variables["TARBALL_PATH"] = (
            os.path.expandvars(self.wlcg_path) + tarball.path
        )
        # Include path to env tarball if env not in cvmfs
        if env_dict[self.ENV_NAME] == "False":
            config.render_variables["TARBALL_ENV_PATH"] = (
                os.path.expandvars(self.wlcg_path) + tarball_env.path
            )
        config.render_variables["LOCAL_TIMESTAMP"] = startup_time
        config.render_variables["LOCAL_PWD"] = startup_dir
        # only needed for $ANA_NAME=ML_train see setup.sh line 158
        if os.getenv("MODULE_PYTHONPATH"):
            config.render_variables["MODULE_PYTHONPATH"] = os.getenv(
                "MODULE_PYTHONPATH"
            )
        return config


# Class to shorten lookup times for large amounts of output targets
#    puppet_task: Task to be run
# Output targets of puppet are saved to the checkfile after puppet is run
# If output targets of puppet don't match with saved targets, checkfile is removed
class PuppetMaster(Task):
    puppet_task = luigi.TaskParameter(description="Task to be supervised.")
    fulltask = luigi.BoolParameter(
        default=False, description="Whether the full puppet task should be displayed."
    )

    # Requirements are the same as puppet task
    def requires(self):
        return self.puppet_task.requires()

    def output(self):
        puppet = self.puppet_task
        # Construct output filename from class name of puppet and identifier
        class_name = puppet.__class__.__name__
        unique_par_str = "_".join([class_name, self.task_id])
        filename = unique_par_str + ".json"
        target = self.local_target(filename)
        # Check if existing file matches with new file
        if target.exists():
            out = puppet.output()
            if isinstance(out, DotDict) and "collection" in out.keys():
                out = out["collection"]
            target_paths = set([targ.path for targ in flatten_collections(out)])
            target_paths_from_file = set(target.load())
            if target_paths != target_paths_from_file:
                # Remove old file if not
                console.log("Missmatch in output files found. Removing checkfile.")
                console.log(
                    list(target_paths_from_file - target_paths)
                    + list(target_paths - target_paths_from_file)
                )
                target.remove()
                if target.exists():
                    raise Exception("File {} could not be deleted".format(target.path))
        return target

    def repr(self, all_params=False, color=None, **kwargs):
        representation = super(PuppetMaster, self).repr(all_params, color, **kwargs)
        if self.fulltask:
            representation += " of " + self.puppet_task.repr(
                all_params, color, **kwargs
            )
        return representation

    def run(self):
        puppet = self.puppet_task
        # Add puppet to shedduler
        # PuppetMaster Tasks restarts after yield
        print("Add task to shedduler: ", puppet)
        yield puppet
        # Write output targets of puppet to PuppetMaster output target
        out = puppet.output()
        if isinstance(out, DotDict) and "collection" in out.keys():
            out = out["collection"]
        target_paths = [targ.path for targ in flatten_collections(out)]
        self.output().dump(target_paths, formatter="json")

    # Get outputs of puppet (Used in non-workflow)
    def give_puppet_outputs(self):
        return self.puppet_task.output()
