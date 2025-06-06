# Apply PEP-328 to Python 2 to unify import directive syntax
# between Python versions
# https://www.python.org/dev/peps/pep-0328
from __future__ import absolute_import

import argparse
import logging
import glob
import os
from pprint import pformat
import re
import shutil
import sys
import traceback

from build_migrator.common.algorithm import flatten_list
from build_migrator.common.os_ext import get_host_system_name, get_platform, Unix

from build_migrator.parsers.build_log_parser import (
    BuildLogParserContext as ParserContext,
)
from ..helpers import (
    get_minified_target,
    get_minified_targets,
    resolve_properties,
    remove_value_from_property,
    ModuleTypes,
    get_target_output_dir,
)
from build_migrator.modules import EntryPoint, Generator
from build_migrator.helpers import filter_top_level_targets

from build_migrator.generators._cmake.cmake_class import CMakeClass
from build_migrator.generators._cmake.cmake_cmd import CMakeCmd
from build_migrator.generators._cmake.cmake_copy import CMakeCopy
from build_migrator.generators._cmake.cmake_fix_multiple_source_instances_with_different_flags import (
    CMakeFixMultipleSourceInstancesWithDifferentFlags,
)
from build_migrator.generators._cmake.cmake_module import CMakeModule
from build_migrator.generators._cmake.cmake_module_copy import CMakeModuleCopy
from build_migrator.generators._cmake.cmake_move_dependencies_from_source_to_target import (
    CMakeMoveDependenciesFromSourceToTarget,
)
from build_migrator.generators._cmake.cmake_variable import CMakeVariable
from build_migrator.generators._cmake.remove_redundant_directory_targets import (
    CMakeRemoveRedundantDirectoryTargets,
)
from build_migrator.generators._cmake.merge_object_libraries_with_same_arguments import (
    MergeObjectLibrariesWithSameArguments,
)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)


class CMakeContext(EntryPoint, Generator):
    glob_file_limit = 3
    cflags_placeholder = "@cflags@"
    main_languages = {"C": "C", "C++": "CXX"}
    extra_languages = {
        "RC": "RC",
        "NASM": "ASM_NASM",
        "MASM": "ASM_MASM",
        "GASM": "ASM",
    }
    language_flag_properties = {
        "c_flags": "C",
        "cxx_flags": "CXX",
        "gasm_flags": "ASM",
        "masm_flags": "ASM_MASM",
        "nasm_flags": "ASM_NASM",
        "rc_flags": "RC",
        "yasm_flags": "ASM_YASM",
    }
    language_include_properties = {
        "c_include_dirs": "C",
        "cxx_include_dirs": "CXX",
        "gasm_include_dirs": "ASM",
        "masm_include_dirs": "ASM_MASM",
        "nasm_include_dirs": "ASM_NASM",
        "rc_include_dirs": "RC",
    }
    build_dir_placeholder = ParserContext.build_dir_placeholder
    source_dir_placeholder = ParserContext.source_dir_placeholder

    @classmethod
    def add_arguments(cls, arg_parser):
        try:
            arg_parser.add_argument(
                "--platform",
                choices=["linux", "windows", "darwin"],
                help="Platform under which the build log was obtained. "
                "Mac and Linux builds can be parsed on any platform. "
                "Windows build logs can be parsed only on Windows. "
                "Default: current platform."
            )
        except argparse.ArgumentError:
            # Already added somewhere else
            # TODO: make a better solution for cases when multiple extensions require the same argument
            pass
        arg_parser.add_argument(
            "--cmake_project_name",
            metavar="NAME",
            help="Value of project(PROJECT-NAME) argument."
        )
        arg_parser.add_argument(
            "--cmake_project_version",
            metavar="VERSION",
            help="Value of project(VERSION) argument.",
        )
        arg_parser.add_argument(
            "--rename",
            metavar=("REGEX", "REPL"),
            nargs=2,
            action="append",
            dest="rename_patterns",
            help="Modify automatic CMake target names.",
        )
        arg_parser.add_argument(
            "--flat_build_dir",
            action="store_true",
            help="Ignore output subdirectories for module targets. For example, "
            "@build_dir@/1/2/3/libfoo.a becomes @build_dir@/libfoo.a.",
        )
        arg_parser.add_argument(
            "--build_dir",
            metavar="DIR",
            nargs="+",
            help="Optional build directory or directories. Default: '/_build'."
        )
        arg_parser.add_argument(
            "--qt_version",
            choices=["5", "6"],
            default="5",
            help="Qt version to use (5 or 6). Default: 5."
        )
        arg_parser.add_argument(
            "--qt_components",
            metavar="COMPONENT",
            nargs="+",
            help="Qt components to include (e.g., Core Gui Widgets)."
        )

    def __init__(
        self,
        build_migrator,
        out_dir,
        cmake_project_name=None,
        cmake_project_version=None,
        prebuilt_subdir=None,
        rename_patterns=None,
        source_subdir=None,
        platform=None,
        flat_build_dir=None,
        build_dir=None,
        qt_version="5",
        qt_components=None,
    ):
        assert os.path.exists(out_dir)
        if platform is None:
            platform = get_host_system_name()

        self.platform_name = platform
        self.platform = get_platform(platform)
        self.project_name = cmake_project_name
        self.out_dir = out_dir
        self.project_version = cmake_project_version
        if source_subdir is None:
            source_subdir = "source"
        self.source_subdir = source_subdir
        if prebuilt_subdir is None:
            prebuilt_subdir = "prebuilt"
        self.prebuilt_subdir = prebuilt_subdir
        if self.prebuilt_subdir == self.source_subdir:
            raise ValueError("prebuilt_subdir cannot be the same as source_subdir")
        self.src_list_subdir = None
        self.placeholders = [
            self.build_dir_placeholder,
            self.source_dir_placeholder,
            self.cflags_placeholder,
        ]

        self.qt_version = qt_version
        self.qt_components = qt_components or []
        self.qt_enabled = bool(self.qt_components)

        self.rename_patterns = []
        for pattern, repl in rename_patterns or []:
            self.rename_patterns.append((re.compile(pattern), repl))

        # Replace invalid characters in target names
        self.rename_patterns.append((re.compile(r"[^a-zA-Z0-9_\.+-]"), "_"))

        if self.project_name:
            self.source_dir_var = self.project_name.upper() + "_SOURCE_DIR"
        else:
            self.source_dir_var = "SOURCE_DIR"

        self.vars = {
            self.cflags_placeholder: "CMAKE_C_FLAGS",
        }
        self.values = {
            self.build_dir_placeholder: "",
            self.cflags_placeholder: "${CMAKE_C_FLAGS}",
            self.source_dir_placeholder: "",
        }
        self.substitutions = {
            self.build_dir_placeholder: "",
            self.cflags_placeholder: "@CMAKE_C_FLAGS@",
            self.source_dir_placeholder: "",
        }

        if build_dir is None:
            build_dir = self.format(self.out_dir)
        else:
            build_dir = build_dir[0]

        self.target_index = {}
        self.yasm_global_compile_flags = {}
        self.nasm_global_compile_flags = {}
        self._builtin_generators = {
            "file": self._generate_for_file,
            "directory": self._generate_for_directory,
            "custom_command": self._generate_for_custom_command,
            "custom_target": self._generate_for_custom_target,
            "installs": self._generate_for_installs,
            "subproject": self._generate_for_subproject,
            "include": self._generate_for_include,
            "conditions": self._generate_for_conditions,
        }
        self.flat_build_dir = flat_build_dir

        # Default C++ standard settings
        self.cxx_standard = "17"
        self.cxx_standard_required = "ON"
        self.cxx_extensions = "OFF"

    def open(self, path, *args, **kwargs):
        assert not os.path.isabs(path), path
        full_path = os.path.join(self.out_dir, path)
        basedir = os.path.split(full_path)[0]
        if basedir and not os.path.exists(basedir):
            os.makedirs(basedir)
        return open(full_path, *args, **kwargs)

    @classmethod
    def escape_special_chars(cls, s):
        if s:
            s = s.replace("\t", r"\t")
            s = s.replace("\n", r"\n")
            s = s.replace("\r", r"\r")
        return s

    @classmethod
    def quote(cls, s, force=False):
        skip_slash = False
        if not force:
            if not s:
                return '""'
            if s.startswith('"') and s.endswith('"'):
                return cls.escape_special_chars(s)
            s = s.replace("\\", "\\\\")
            skip_slash = True
            if s.find(" ") == -1:
                return cls.escape_special_chars(s)
        if not skip_slash:
            s = s.replace("\\", "\\\\")
        s = '"' + s.replace('"', '\\"') + '"'
        return cls.escape_special_chars(s)

    def format_location(self, location, prepend_var=True):
        if prepend_var:
            location = location
        return self.quote(location)

    def format_cache_variable(self, name, value=None, type="STRING"):
        return self.format_variable(name, value, ["CACHE", type, '""'])

    def format_variable(self, name, value=None, suffix_args=None):
        if not value:
            value = ""

        if not isinstance(value, list):
            value = [value]

        return self.format_call("set", [name], value, suffix_args)

    def _process_yasm_global_compile_flags(self, file, flags, target_index=None):
        local_flags = []
        variables = set()
        for f in flags:
            if f.startswith("@") and f.endswith("@"):
                variables.add(f)
            var_name = None
            if f.startswith("-a"):
                var_name = "CMAKE_ASM_YASM_ARCHITECTURE"
            elif f.startswith("-f"):
                var_name = "CMAKE_ASM_YASM_OBJECT_FORMAT"
            elif f.startswith("-m"):
                var_name = "CMAKE_ASM_YASM_MACHINE"
            else:
                local_flags.append(f)

            if var_name:
                value = f[2:]
                existing_value = self.yasm_global_compile_flags.get(var_name)
                if existing_value is not None:
                    assert value == existing_value, value + " vs " + existing_value
                else:
                    file.write(self.format_variable(var_name, value))
                    self.yasm_global_compile_flags[var_name] = value

        return local_flags, variables

    def _process_nasm_global_compile_flags(self, file, flags):
        local_flags = []
        variables = set()
        for f in flags:
            if f.startswith("@") and f.endswith("@"):
                variables.add(f)
            var_name = None
            if f.startswith("-f"):
                var_name = "CMAKE_ASM_NASM_OBJECT_FORMAT"
            else:
                local_flags.append(f)

            if var_name:
                value = f[2:]
                existing_value = self.nasm_global_compile_flags.get(var_name)
                if existing_value is not None:
                    assert value == existing_value, value + " vs " + existing_value
                else:
                    file.write(self.format_variable(var_name, value))
                    self.nasm_global_compile_flags[var_name] = value

        return local_flags, variables

    def _rename_target(self, target):
        if "name" not in target:
            return
        name = target["name"]
        for pattern, replacement in self.rename_patterns:
            name = pattern.sub(replacement, name)
        if name != target["name"]:
            logger.debug("Renaming %r to %r" % (target["name"], name))
            target["name"] = name

    # TODO: use resolve_compile_flags, remove_property_value
    def _process_nasm_yasm_sources(self, f, targets, yasm_sources, nasm_sources):
        vars_in_nasm_flags = set()
        vars_in_yasm_flags = set()
        for yasm_source in yasm_sources:
            (
                yasm_source["compile_flags"],
                variables,
            ) = self._process_yasm_global_compile_flags(f, yasm_source["compile_flags"])
            vars_in_yasm_flags.update(variables)
        for nasm_source in nasm_sources:
            (
                nasm_source["compile_flags"],
                variables,
            ) = self._process_nasm_global_compile_flags(f, nasm_source["compile_flags"])
            vars_in_nasm_flags.update(variables)
        for t in targets:
            if t["type"] not in ["module", "class"]:
                continue
            # process global classes with nasm/yasm flags
            if t["type"] == "class" and not t["conditions"]:
                t = t["properties"]
            if "yasm_flags" in t:
                t["yasm_flags"], variables = self._process_yasm_global_compile_flags(
                    f, t["yasm_flags"]
                )
                vars_in_yasm_flags.update(variables)
            if "nasm_flags" in t:
                t["nasm_flags"], variables = self._process_nasm_global_compile_flags(
                    f, t["nasm_flags"]
                )
                vars_in_nasm_flags.update(variables)

        yasm_only_vars = vars_in_yasm_flags - vars_in_nasm_flags
        for vt in [self.target_index[v] for v in yasm_only_vars]:
            remaining_values, variables = self._process_yasm_global_compile_flags(
                f, vt["value"]
            )
            assert len(variables) == 0, variables
            vt["value"] = remaining_values

        nasm_only_vars = vars_in_nasm_flags - vars_in_yasm_flags
        for vt in [self.target_index[v] for v in nasm_only_vars]:
            remaining_values, variables = self._process_nasm_global_compile_flags(
                f, vt["value"]
            )
            assert len(variables) == 0, variables
            vt["value"] = remaining_values

        yasm_nasm_vars = vars_in_yasm_flags & vars_in_nasm_flags
        for vt in [self.target_index[v] for v in yasm_nasm_vars]:
            nasm_remaining_values, variables = self._process_nasm_global_compile_flags(
                f, vt["value"]
            )
            assert len(variables) == 0, variables
            yasm_remaining_values, variables = self._process_yasm_global_compile_flags(
                f, vt["value"]
            )
            assert len(variables) == 0, variables
            assert nasm_remaining_values == yasm_remaining_values
            vt["value"] = yasm_remaining_values

    @staticmethod
    def _remove_link_flag_prefix(s):
        if s.startswith("-Wl,"):
            s = s[4:]
        return s

    mt_re = re.compile("[-/]MTd?")
    md_re = re.compile("[-/]MDd?")

    def process_compile_flags(self, flags):
        if not flags:
            return flags
        if isinstance(flags, str):
            return self.process_compile_flags([flags])[0]
        if self.platform_name == "windows" and flags:
            fixed_flags = []
            for f in flags:
                if isinstance(f, str):
                    if self.mt_re.match(f):
                        f = "/MT$<$<CONFIG:Debug>:d>"
                    if self.md_re.match(f):
                        f = "/MD$<$<CONFIG:Debug>:d>"
                if isinstance(f, list) or isinstance(f, tuple):
                    f = "SHELL:{}".format(" ".join(f))
                fixed_flags.append(f)
            flags = fixed_flags
        return flags

    def _process_version_properties(self, target_index):
        remove_flags = []
        for t in target_index.values():
            if t["type"] != "module":
                continue
            module_types = [ModuleTypes.executable, ModuleTypes.shared_lib]
            if t["module_type"] not in module_types:
                continue

            flags = resolve_properties(t, target_index, "link_flags")
            if not flags:
                continue
            for f in flags:
                if isinstance(f, list):
                    if len(f) != 2:
                        continue
                    flag_name = self._remove_link_flag_prefix(f[0])
                    value = self._remove_link_flag_prefix(f[1])
                else:
                    tmp = self._remove_link_flag_prefix(f).split(",")
                    if len(tmp) != 2:
                        tmp = self._remove_link_flag_prefix(f).split("=")
                        if len(tmp) != 2:
                            continue
                    flag_name, value = tmp[0], tmp[1]
                if flag_name == "-soname":
                    descr = Unix.parse_shared_lib(value)
                    t["compatibility_version"] = descr.get("version")
                elif flag_name == "-compatibility_version":
                    t["compatibility_version"] = value
                elif flag_name == "-current_version":
                    t["version"] = value
                else:
                    continue
                remove_flags.append(f)

        for f in remove_flags:
            remove_value_from_property(target_index, "link_flags", f)

    def _target_is_in_source_dir(self, target):
        return target["output"].startswith(self.source_dir_placeholder)

    def _target_is_in_build_dir(self, target):
        return target["output"].startswith(self.build_dir_placeholder)
    
    def _process_qt_sources(self, file, targets, qt_sources):
        qt_defines = [
            "-DQT_NO_DEBUG",
            "-DQT_WIDGETS_LIB",
            "-DQT_GUI_LIB",
            "-DQT_CORE_LIB",
            "-DQT_NETWORK_LIB",
        ]
        qt_include_prefixes = [
            "/usr/include/x86_64-linux-gnu/qt5",
            "/usr/lib/x86_64-linux-gnu/qt5/mkspecs",
        ]
        qt_lib_pattern = re.compile(r"/usr/lib/x86_64-linux-gnu/libQt5(\w+)\.so")
        for target in targets:
            if target["type"] == "module":
                # Remove moc_ and ui_ sources, as they will be handled by AUTOMOC/AUTOUIC
                target["sources"] = [
                    s for s in target.get("sources", [])
                    if not (
                        s["path"].endswith(".cpp") and os.path.basename(s["path"]).startswith("moc_") or
                        s["path"].endswith(".h") and os.path.basename(s["path"]).startswith("ui_")
                    )
                ]
                # Add .ui file to sources
                for source in qt_sources:
                    if source.get("extension") == ".ui":
                        target["sources"].append(source)
                # Clean up compile flags and include dirs
                for source in target.get("sources", []):
                    if "compile_flags" in source:
                        source["compile_flags"] = [
                            f for f in source["compile_flags"] if f not in qt_defines
                        ]
                    if "include_dirs" in source:
                        source["include_dirs"] = [
                            d for d in source["include_dirs"]
                            if not any(d.startswith(p) for p in qt_include_prefixes)
                        ]
                if "libs" in target:
                    # Map Qt libs to Qt5:: targets, deduplicate, keep others
                    unique_libs = []
                    seen = set()
                    for lib in target["libs"]:
                        match = qt_lib_pattern.match(lib)
                        if match:
                            component = match.group(1)
                            lib = f"Qt5::{component}"
                        elif lib == "GL":
                            lib = "OpenGL::GL"
                        elif lib == "pthread":
                            lib = "Threads::Threads"
                        if lib not in seen:
                            unique_libs.append(lib)
                            seen.add(lib)
                    target["libs"] = unique_libs

    def initialize_cmakelist(self, targets):
        languages = set()

        yasm_sources = []
        nasm_sources = []
        qt_sources = []
        class_id = 1
        modules_found = False
        prebuilt_file_counter = 0
        std_re = re.compile(r"-std=(c\+\+|gnu\+\+)(11|14|17|20|23|1y|0x|98)", re.IGNORECASE)
        config_std_re = re.compile(r"c\+\+(\d+)", re.IGNORECASE)
        valid_standards = {"98", "11", "14", "17", "20", "23"}
        std_map = {"0x": "11", "1y": "14"}  # Map old standards (e.g., gnu++1y -> 14)

        for target in targets:
            self._rename_target(target)
            target_output_list = self._get_target_output_list(target)
            for output in target_output_list:
                if output in self.target_index:
                    raise ValueError("Duplicate target output %r" % output)
                self.target_index[output] = target
            if not target_output_list:
                assert target["type"] == "class"
                self.target_index[class_id] = target
                class_id += 1
            if target["type"] == "file" and self._target_is_in_build_dir(target):
                prebuilt_file_counter += 1
            if target["type"] == "module":
                for s in target["sources"]:
                    lang = s.get("language")
                    if lang:
                        languages.add(lang)
                    if lang == "YASM":
                        yasm_sources.append(s)
                    if lang == "NASM":
                        nasm_sources.append(s)
                    if s.get("extension") in [".ui", ".qrc"]:
                        qt_sources.append(s)
                    # Check compile_flags in sources
                    for flag in s.get("compile_flags", []):
                        match = std_re.match(flag)
                        if match:
                            std = match.group(2).lower()
                            std = std_map.get(std, std)  # Map 1y -> 14, 0x -> 11
                            if std in valid_standards and std != self.cxx_standard:
                                self.cxx_standard = std
                                self.cxx_extensions = "ON" if match.group(1).lower() == "gnu++" else "OFF"
                modules_found = True
                # Check for C++ standard in module targets
                for flag_key in ["cxxflags", "cxxflags_release", "cxxflags_debug"]:
                    if flag_key in target:
                        for flag in target.get(flag_key, []):
                            match = std_re.match(flag)
                            if match:
                                std = match.group(2).lower()
                                std = std_map.get(std, std)
                                if std in valid_standards and std != self.cxx_standard:
                                    self.cxx_standard = std
                                    self.cxx_extensions = "ON" if match.group(1).lower() == "gnu++" else "OFF"
                if "config" in target:
                    config = target["config"]
                    if isinstance(config, str):
                        config = config.split()
                    for item in config:
                        match = config_std_re.match(item)
                        if match:
                            std = match.group(1)
                            if std in valid_standards and std != self.cxx_standard:
                                self.cxx_standard = std
                                # Assume extensions OFF unless gnu++ found in cxxflags

        if self.prebuilt_subdir not in (None, "."):
            self.copy_prebuilt_files_using_glob = (
                prebuilt_file_counter > self.glob_file_limit
            )
            if self.copy_prebuilt_files_using_glob:
                self.prebuilt_files_copied = False
        else:
            self.copy_prebuilt_files_using_glob = False

        if len(languages - set(["YASM"])) == 0 and modules_found:
            # We're probably going to use the linker.
            # At least one language must be enabled for that to work.
            languages.add("C")

        main_languages = sorted(
            [
                self.main_languages[lang]
                for lang in languages
                if lang in self.main_languages
            ]
        )
        extra_languages = sorted(
            [
                self.extra_languages[lang]
                for lang in languages
                if lang in self.extra_languages
            ]
        )
        self.languages = main_languages + extra_languages

        with self.open("CMakeLists.txt", "w") as f:
            f.write(
                self.format_header(
                    main_languages=main_languages,
                    extra_languages=extra_languages,
                    use_glob=self.copy_prebuilt_files_using_glob,
                )
            )

            qt_lib_pattern = re.compile(r"/usr/lib/x86_64-linux-gnu/libQt5(\w+)\.so")
            qt_modules_used = set()

            for t in targets:
                for lib in t.get("libs", []):
                    match = qt_lib_pattern.match(lib)
                    if match:
                        qt_modules_used.add(match.group(1))

            if qt_modules_used:
                f.write("set(CMAKE_AUTOMOC ON)\n")
                f.write("set(CMAKE_AUTOUIC ON)\n")
                f.write("set(CMAKE_AUTORCC ON)\n")
                f.write("\n")
                modules_str = " ".join(sorted(qt_modules_used))
                f.write(f"find_package(Qt5 COMPONENTS {modules_str} REQUIRED)\n")
            if any("GL" in t.get("libs", []) or "OpenGL::GL" in t.get("libs", []) for t in targets):
                f.write("find_package(OpenGL REQUIRED)\n")
            if any("pthread" in t.get("libs", []) or "Threads::Threads" in t.get("libs", []) for t in targets):
                f.write("find_package(Threads REQUIRED)\n")
            f.write("\n")

            self._process_nasm_yasm_sources(f, targets, yasm_sources, nasm_sources)
            self._process_qt_sources(f, targets, qt_sources)
            self._process_version_properties(self.target_index)

    def finalize_cmakelists(self):
        with self.open("CMakeLists.txt", "a") as f:
            f.write(self.format_footer())
        with self.open("extensions.cmake", "w") as f:
            path = os.path.join(SCRIPT_DIR, "_cmake/files/extensions.cmake")
            shutil.copy(path, self.out_dir)

    def get_copy_origin(self, source):
        target = self.target_index.get(source)
        if target is None:
            return None
        if not target["output"].startswith(self.build_dir_placeholder):
            return None
        if target["type"] not in ["copy", "module_copy"]:
            return target
        else:
            return self.get_copy_origin(target["source"])

    def format_call(
        self,
        func,
        begin_formal_args,
        argv=None,
        end_formal_args=None,
        argv_from_file_var=None,
    ):
        if argv is None:
            argv = []
        if end_formal_args is None:
            end_formal_args = []
        assert isinstance(begin_formal_args, list), begin_formal_args
        assert isinstance(argv, list), argv
        assert isinstance(end_formal_args, list), end_formal_args

        get_string_from_file = ""

        begin_str = self.format(func + "(" + " ".join(begin_formal_args))
        end_str = ""
        if end_formal_args:
            end_str = " " + self.format(" ".join(end_formal_args))

        argv_str = ""
        if begin_formal_args:
            argv_str = " "
        argv = [self.quote(a) for a in argv]
        if argv_from_file_var and len(argv) > 30:
            path = argv_from_file_var + ".cmake"
            if self.src_list_subdir not in (None, "."):
                path = self.src_list_subdir + "/" + path
            with self.open(path, "w") as f:
                f.write("set({}\n".format(argv_from_file_var))
                for arg in argv:
                    if arg in ["COMMAND", "DEPENDS"]:
                        f.write(self.format(arg, escape_slash=False) + " ")
                    else:
                        f.write(self.format(arg, escape_slash=False) + "\n")
                f.write(")\n")
            get_string_from_file = "include({})\n".format(
                self.format_location(path, False)
            )
            argv_str += "${" + argv_from_file_var + "}"
        else:
            argv_str += self.format(" ".join(argv), escape_slash=False)
        
        if "COMMAND" in argv_str:
            index = argv.index("DEPENDS")
            command = argv[:index]
            depends = argv[index:]
            argv_str = "\n    " + self.format(" ".join(command), escape_slash=False) + "\n    " + self.format(" ".join(depends), escape_slash=False) + "\n"
            if end_formal_args:
                end_str = "   " + end_str + "\n"
            return get_string_from_file + begin_str + argv_str + end_str + ")\n"

        if len(argv_str) > 60:
            argv_str = "\n    " + self.format("\n    ".join(argv), escape_slash=False) + "\n"
            if end_formal_args:
                end_str = "   " + end_str + "\n"

        return get_string_from_file + begin_str + argv_str + end_str + ")\n"

    def format(self, str_fmt, escape_slash=True, **kwargs):
        if kwargs:
            str_fmt = str_fmt.format(**kwargs)
        for placeholder in self.placeholders:
            str_fmt = str_fmt.replace(placeholder + "/", self.values[placeholder])
        if escape_slash:
            # supported escape sequences: \r, \t, \n, \"
            return re.sub(r'\\(?!["trn])', r"\\\\", str_fmt)
        else:
            return str_fmt

    def format_executable(self, name, sources):
        sources_var_name = name.replace(".", "_").upper() + "_SRC"
        return self.format_call(
            "add_executable", [name], sources, argv_from_file_var=sources_var_name
        )

    def format_shared_library(self, name, sources):
        sources_var_name = name.replace(".", "_").upper() + "_SRC"
        return self.format_call(
            "add_library",
            [name, "SHARED"],
            sources,
            argv_from_file_var=sources_var_name,
        )

    def format_static_library(self, name, sources):
        sources_var_name = name.replace(".", "_").upper() + "_SRC"
        return self.format_call(
            "add_library",
            [name, "STATIC"],
            sources,
            argv_from_file_var=sources_var_name,
        )

    def format_object_library(self, name, sources):
        sources_var_name = name.replace(".", "_").upper() + "_SRC"
        return self.format_call(
            "add_library",
            [name, "OBJECT"],
            sources,
            argv_from_file_var=sources_var_name,
        )
        
    def format_interface(self, name, sources):
        return self.format_call(
            "add_library",
            [name, "INTERFACE"]
        )

    def format_link_flags(self, target_name, flags):
        return self.format_call(
            "target_link_options", [target_name, "PRIVATE"], flatten_list(flags)
        )
    
    def format_target_output_subdir(self, target_name, output_dir):
        return self.format_call(
            "set_target_output_subdir",
            [target_name, "RUNTIME_OUTPUT_DIRECTORY", self.quote(output_dir)]
        )

    def format_header(
        self, main_languages=None, extra_languages=None, includes=None, use_glob=False
    ):
        result = "cmake_minimum_required(VERSION 3.13)\n\n"
        project_version = self.project_version
        project_name = self.project_name
        if project_version is not None:
            project_version = " VERSION {}".format(project_version)
        else:
            project_version = ""
        if main_languages:
            main_languages_str = " ".join(main_languages)
            if project_version:
                main_languages_str = "LANGUAGES " + main_languages_str
        else:
            # This disables C and CXX
            main_languages_str = "LANGUAGES"
        result += self.format(
            "project({name} {languages}{project_version})\n",
            name=project_name or "PROJECT",
            languages=main_languages_str,
            project_version=project_version,
        )
        for lang in extra_languages:
            result += self.format("enable_language({lang})\n", lang=lang)
        result += "\n"
        if "CXX" in main_languages:
            result += f"set(CMAKE_CXX_STANDARD {self.cxx_standard})\n"
            result += f"set(CMAKE_CXX_STANDARD_REQUIRED {self.cxx_standard_required})\n"
            result += f"set(CMAKE_CXX_EXTENSIONS {self.cxx_extensions})\n\n"
        includes = ["include({})".format(inc) for inc in (includes or [])]
        result += "\n".join(includes)
        if includes:
            result += "\n"
        result += "list(APPEND CMAKE_MODULE_PATH ${CMAKE_CURRENT_LIST_DIR})\n"
        result += "include(extensions)\n\n"
        return result

    def format_footer(self):
        return ""

    def join_property_values(self, values, **kwargs):
        values_str = '""'
        if len(values) == 1:
            val = values[0]
            # variable may contain a list, so it should be enclosed in quotes
            force = val.startswith("@") and val.endswith("@")
            values_str = self.quote(val, force=force)
        elif len(values) > 1:
            values_str = self.quote(";".join(values), force=True)
        return self.format(values_str, **kwargs)

    def _get_target_output_list(self, target):
        result = []
        if "output" in target:
            result.append(target["output"])
        for output in target.get("msvc_import_lib") or []:
            result.append(output)
        return result

    def _get_skip_set_for_add_dependencies(self, target):
        # In some cases, we don't need to call add_dependencies(t1 t2) explicitly:
        # target_link_libraries(t1 t2)
        # add_library(t1 $<TARGET_OBJECTS:t2>)

        skip_targets = []
        if target.get("type") == "module":
            for lib in target.get("libs") or []:
                if isinstance(lib, dict):
                    # { gcc_whole_archive: True, value: output }
                    lib = lib["value"]
                if lib not in self.target_index:
                    continue
                dep_target = self.target_index[lib]
                if dep_target["type"] != "cmd":
                    skip_targets.append(dep_target)
            for obj in target.get("objects") or []:
                if obj not in self.target_index:
                    continue
                dep_target = self.target_index[obj]
                if dep_target["type"] != "cmd":
                    skip_targets.append(dep_target)

        skip_set = set()
        for skip_target in skip_targets:
            for output in self._get_target_output_list(skip_target):
                skip_set.add(output)
        return skip_set

    def format_dependencies(self, target):
        skip_set = self._get_skip_set_for_add_dependencies(target)
        deps = set()
        for dep_out in target.get("dependencies") or []:
            if dep_out.startswith(ParserContext.source_dir_placeholder):
                continue
            if dep_out in skip_set:
                continue
            if dep_out not in self.target_index:
                continue

            dep_target = self.target_index[dep_out]
            dep_has_name = dep_target.get("name") is not None
            if not dep_has_name:
                continue

            dep_is_module = dep_target.get("type") == "module"
            dep_is_cmd = dep_target.get("type") == "cmd"
            dep_is_file = dep_target.get("type") == "file"
            if not (dep_is_cmd or dep_is_module or dep_is_file):
                continue

            deps.add(dep_target["name"])
        if deps:
            return self.format_call(
                "add_dependencies", [target["name"]], sorted(list(deps))
            )
        else:
            return ""

    def get_configurable_content(self, content):
        for placeholder in self.placeholders:
            content = content.replace(placeholder, self.substitutions[placeholder])

        return content

    def generate(self, targets, generators):
        if self.flat_build_dir:
            for target in targets:
                if target["type"] != "module":
                    continue

                output_dir = get_target_output_dir(target)
                if output_dir in target["dependencies"]:
                    # deduplicate dependencies
                    target["dependencies"] = list(set(target["dependencies"]))
                    # remove output directory dependency
                    target["dependencies"].remove(output_dir)

        optimizers = list(filter(lambda g: hasattr(g, "optimize"), generators))
        generators = list(filter(lambda g: hasattr(g, "generate"), generators))

        for optimizer in optimizers:
            logger.debug(type(optimizer).__name__)
            try:
                result = optimizer.optimize(targets)
                if result != targets:
                    logger.debug(" > Modified:")
                    logger.debug(pformat(get_minified_targets(result)))
                targets = result
            except Exception:
                logging.error(traceback.format_exc())

        targets = filter_top_level_targets(targets)

        self.initialize_cmakelist(targets)
        for target in targets:
            if target.get("skip"):
                # custom BOM attribute for cmake generator
                # used by CMakeRemoveRedundantDirectoryTargets
                logger.debug(" > Skipping target due to 'skip' attribute:")
                logger.debug(pformat(get_minified_target(target)))
                continue
            logger.debug(" > Generate CMake for target:")
            logger.debug(pformat(get_minified_target(target)))
            success = False
            builtin_generator = self._builtin_generators.get(target["type"])
            if builtin_generator:
                builtin_generator(target)
                success = True
            else:
                for generator in generators:
                    logger.debug(type(generator).__name__)
                    try:
                        if generator.generate(target):
                            success = True
                            break
                    except Exception:
                        logging.error(traceback.format_exc())
            assert success, target
        self.finalize_cmakelists()

    def _generate_for_directory(self, target):
        if self._target_is_in_source_dir(target):
            return
        output = target["output"]
        if output == self.build_dir_placeholder:
            return
        with self.open("CMakeLists.txt", "a") as f:
            f.write(
                self.format(
                    "file(MAKE_DIRECTORY {output})\n", output=self.quote(output)
                )
            )

    def _generate_for_custom_command(self, target):
        with self.open("CMakeLists.txt", "a") as f:
            dependencies = []
            for dep in target.get("dependencies", []):
                if dep in self.target_index:
                    dep_target = self.target_index[dep]
                    if dep_target.get("name"):
                        dependencies.append(dep_target["name"])
                    else:
                        dependencies.append(dep)
                else:
                    dependencies.append(dep)
            commands = target.get("commands", [target["command"]])
            argv = []
            for cmd in commands:
                argv.extend(["COMMAND", cmd])
            for dpnd in dependencies:
                argv.extend(["DEPENDS", dpnd])
            f.write(self.format_call(
                "add_custom_command",
                ["OUTPUT", self.quote(target["output"])],
                argv=argv,
                end_formal_args=["VERBATIM"]
            ) + "\n")

    def _generate_for_custom_target(self, target):
        with self.open("CMakeLists.txt", "a") as f:
            dependencies = []
            for dep in target.get("dependencies", []):
                if dep in self.target_index:
                    dep_target = self.target_index[dep]
                    if dep_target.get("name"):
                        dependencies.append(dep_target["name"])
                    else:
                        dependencies.append(dep)
                else:
                    dependencies.append(dep)
            commands = target.get("commands", [])
            argv = []
            for cmd in commands:
                argv.extend(["COMMAND", cmd])
            for dpnd in dependencies:
                argv.extend(["DEPENDS", dpnd])
            f.write(self.format_call(
                "add_custom_target",
                [target["name"]],
                argv=argv,
                end_formal_args=["VERBATIM"]
            ) + "\n")
        
    def _generate_for_installs(self, target):
        args_d = ["DIRECTORY"]
        args_f = ["FILES"]
        with self.open("CMakeLists.txt", "a") as f:
            files = []
            for file in target.get("dependencies", []):
                if "*" in file:
                    files_path = os.path.abspath(os.path.join(os.path.dirname(self.out_dir),self.format(file)))
                    for file in glob.glob(files_path):
                        if os.path.isdir(file):
                            args_d.extend([file.replace(os.path.dirname(self.out_dir), "")])
                        else:
                            args_f.extend([file.replace(os.path.dirname(self.out_dir), "")])
                else:
                    args_f.extend([file])

                if file in self.target_index:
                    file_target = self.target_index[file]
                    if file_target.get("name"):
                        files.append(file_target["name"])
                    else:
                        files.append(file)
                else:
                    files.append(file)

            if len(args_d) != 1:
                f.write(self.format_call(
                    "install",
                    args_d,
                    argv=["DESTINATION", self.quote(target["output"])]
                ) + "\n")
            if len(args_f) != 1:
                f.write(self.format_call(
                    "install",
                    args_f,
                    argv=["DESTINATION", self.quote(target["output"])]
                ) + "\n")

    _glob_template = """
set(copy_prebuilt_artifacts_DIR {prebuild_subdir})
set(copy_prebuilt_artifacts_DEST ${{CMAKE_CURRENT_BINARY_DIR}})
file(GLOB_RECURSE _files RELATIVE ${{CMAKE_CURRENT_LIST_DIR}}/${{copy_prebuilt_artifacts_DIR}} ${{copy_prebuilt_artifacts_DIR}}/*)
foreach(_f ${{_files}})
    configure_file(${{copy_prebuilt_artifacts_DIR}}/${{_f}} ${{copy_prebuilt_artifacts_DEST}}/${{_f}} COPYONLY)
endforeach()
"""

    def _generate_for_file(self, target):
        # Skip prebuilt Qt files (ui_*.h, moc_*.cpp) as they are handled by AUTOMOC/AUTOUIC
        if self._target_is_in_build_dir(target):
            filename = os.path.basename(target["output"])
            if filename.startswith("ui_") and filename.endswith(".h"):
                return
            if filename.startswith("moc_") and filename.endswith(".cpp"):
                return
    
        if sys.version_info >= (3, 0) and isinstance(target["content"], str):
            mode = "wt"
        else:
            mode = "wb"

        subdir = None
        if self._target_is_in_build_dir(target):
            location = target["output"][len(self.build_dir_placeholder) + 1:]
            subdir = self.prebuilt_subdir
        elif self._target_is_in_source_dir(target):
            location = target["output"][len(self.source_dir_placeholder) + 1:]
            subdir = self.source_subdir
        else:
            location = target["output"]
            subdir = "external"
        if subdir is not None:
            location = os.path.normpath(subdir + "/" + location)
        location = location.replace("\\", "/").replace("..", "_").replace(":", "_")
        if location[0] == "/":
            location = location[1:]

        with self.open(location, mode) as target_file:
            target_file.write(target["content"])

        if self._target_is_in_source_dir(target):
            return

        with self.open("CMakeLists.txt", "a") as f:
            if self._target_is_in_build_dir(target):
                if self.copy_prebuilt_files_using_glob:
                    if not self.prebuilt_files_copied:
                        s = self.format(
                            self._glob_template, prebuild_subdir=self.prebuilt_subdir
                        )
                        f.write(s)
                        self.prebuilt_files_copied = True
                    return

            s = self.format(
                "configure_file({input} {output} COPYONLY)\n",
                input=self.format_location(location),
                output=self.quote(target["output"]),
            )
            f.write(s)
    
    def _generate_for_include(self, target):
        include, ext = os.path.splitext(self.format(target["output"]))
        with self.open("CMakeLists.txt", "a") as f:
            f.write(self.format_call("include", [], [self.quote(include)]))


    def _generate_for_subproject(self, target):
        """Generate CMake commands for a subproject target."""
        if target["module_type"] != "subdirs":
            logger.warning(f"Skipping subproject with unsupported module_type: {target['module_type']}")
            return

        subdir = target["output"]
        if not subdir:
            logger.warning(f"Could not extract subdirectory from output: {target['output']}")
            return
        
        if "name" not in target:
            target["name"] = subdir.replace("/", "_").replace("\\", "_")
            self._rename_target(target)

        if target["output"] not in self.target_index:
            self.target_index[target["output"]] = target

        with self.open("CMakeLists.txt", "a") as f:
            source_subdir_path = subdir
            f.write(self.format_add_subdirectory(source_subdir_path))
            deps = self._get_subproject_dependencies(target)
            if deps:
                f.write(self.format_call("add_dependencies", [target["name"]], sorted(deps)))

    def resolve_vars(self, value):
        if isinstance(value, str):
            replacements = {
                '$$BINDIR': '/usr/bin',
                '$$DATADIR': '/usr/share',
                '$$PWD': os.path.dirname(self.out_dir),
            }
            for var, repl in replacements.items():
                value = value.replace(var, repl)
            value = re.sub(r'\$\$(\w+)', r'${\1}', value)
            return value
        elif isinstance(value, list):
            return [self.resolve_vars(v) for v in value if v is not None]
        return value

    def _generate_for_conditions(self, target):
        with self.open("CMakeLists.txt", "a") as f:
            f.write("\n")
            self._process_conditions(target.get("project_name", ""), f, target.get("conditions", []), target.get("dependencies", []))

    def _process_conditions(self, project_name, file, conditions, dependencies, space=0):
        for i, cond in enumerate(conditions):
            condition = cond.get("condition", [])
            variables = cond.get("variables", {})
            nested_conditions = cond.get("conditions", [])
            cmake_condition = self._translate_condition_to_cmake(condition)
            if condition == ["    "*space + "else"]:
                file.write("else()\n")
            else:
                file.write("    "*space + f"if({cmake_condition})\n" if cmake_condition else "if(TRUE)\n")
            self._process_variables(project_name, file, variables, dependencies, i, len(conditions), conditions, space)
            if nested_conditions:
                self._process_conditions(project_name, file, nested_conditions, dependencies, space+1)
            next_is_else = (i + 1 < len(conditions)) and conditions[i + 1].get("condition", []) == ["else"]
            if not next_is_else:
                file.write("    "*space + "endif()\n\n")

    def _translate_condition_to_cmake(self, condition):
        if not condition or condition == ["else"]:
            return ""
        conditions = [self._translate_single_condition(c) for c in condition if c is not None]
        return " AND ".join(conditions)

    def _translate_single_condition(self, cond):
        if cond.startswith("isEmpty("):
            var_name = cond[8:-1].strip()
            return f"NOT DEFINED {var_name}"
        elif cond.startswith("exists("):
            path = self.resolve_vars(cond[7:-1].strip())
            return f'EXISTS "{path}"'
        elif cond.startswith("equals("):
            var, value = [x.strip() for x in cond[7:-1].split(",", 1)]
            return f'"{var}" STREQUAL "{value}"'
        elif cond.startswith("contains("):
            var, value = [x.strip() for x in cond[9:-1].split(",", 1)]
            if var == "CONFIG":
                return f'"{value}" IN_LIST CONFIG'
            return f'"{value}" IN_LIST {var}'
        elif cond.startswith("!"):
            return f"NOT {self._translate_single_condition(cond[1:].strip())}"
        elif cond in ["bsd", "linux", "unix", "macx"]:
            return f'"{cond}" IN_LIST CMAKE_SYSTEM_NAME'
        elif cond == "win32":
            return 'CMAKE_SYSTEM_NAME MATCHES "Windows"'
        return f'"{cond}" IN_LIST CONFIG'

    def _process_variables(self, project_name, file, variables, dependencies, cond_index, num_conditions, conditions, space):
        if not hasattr(self, 'source_files'):
            self.source_files = []
        for var_name, var_values in variables.items():
            if var_name is None or var_values is None:
                logger.warning(f"Skipping invalid variable: {var_name}")
                continue
            var_values = self.resolve_vars(var_values)
            var_name_lower = var_name.lower()
            if var_name_lower.endswith(".path"):
                continue
            elif var_name_lower.endswith(".files"):
                install_name = var_name_lower[:-6]
                install_path = variables.get(f"{install_name}.path", ["/usr"])[0]
                install_path = self.resolve_vars(install_path)
                files = []
                for f in var_values:
                    if f is None:
                        continue
                    if "*" in f:
                        files.extend([x.replace(os.path.dirname(self.out_dir), "") 
                                     for x in glob.glob(os.path.abspath(os.path.join(os.path.dirname(self.out_dir), self.format(f))))])
                    else:
                        files.append(f)
                if files:
                    file.write(self.format_call(
                        "    "*space + "    install",
                        ["FILES"] + [self.quote(self.format(f)) for f in files],
                        argv=["DESTINATION", self.quote(self.format(install_path))]
                    ))
            elif var_name_lower in ["sources"]:
                self.source_files.extend(var_values)
                if cond_index == num_conditions - 1 or not any(c.get("variables", {}).get(var_name_lower) for c in conditions[cond_index+1:]):
                    file.write(self.format_call(
                        "    "*space + "    target_sources",
                        [project_name, "PRIVATE"],
                        [self.quote(self.format(f)) for f in self.source_files if f is not None]
                    ))
                    self.source_files = []
            elif var_name_lower in ["libs"]:
                link_dirs = [v[2:] for v in var_values if v is not None and v.startswith("-L")]
                libs = [v[2:] if v.startswith("-l") else v for v in var_values if v is not None and not v.startswith("-L")]
                if link_dirs:
                    file.write(self.format_call("    "*space + "    link_directories", [], [self.quote(self.format(d)) for d in link_dirs]))
                if libs:
                    file.write(self.format_call(
                        "    "*space + "    target_link_libraries",
                        [project_name, "PRIVATE"],
                        [self.quote(self.format(l)) for l in libs]
                    ))
            elif var_name_lower.endswith(".defines"):
                file.write(self.format_call(
                    "    "*space + "    add_definitions",
                    [],
                    [f"-D{self.quote(self.format(v))}" for v in var_values if v is not None]
                ))
            elif var_name_lower.endswith(".includepath"):
                file.write(self.format_call(
                    "    "*space + "    include_directories",
                    [],
                    [self.quote(self.format(v)) for v in var_values if v is not None]
                ))
            else:
                file.write(self.format_call(
                    "    "*space + "    set",
                    [var_name.upper()],
                    [self.quote(self.format(v)) for v in var_values if v is not None]
                ))

    def _get_subproject_dependencies(self, target):
        deps = []
        for dep_output in target.get("dependencies", []):
            if dep_output not in self.target_index:
                logger.debug(f"Dependency not found in target_index: {dep_output}")
                continue
            dep_target = self.target_index[dep_output]
            if dep_target.get("type") != "subproject" or dep_target.get("module_type") != "subdirs":
                logger.debug(f"Skipping non-subproject dependency: {dep_output}")
                continue
            if "name" not in dep_target:
                subdir = self.format(dep_target["output"])
                if subdir:
                    dep_target["name"] = subdir.replace("/", "_").replace("\\", "_")
                    self._rename_target(dep_target)
                else:
                    logger.warning(f"Could not assign name to dependency: {dep_output}")
                    continue
            deps.append(dep_target["name"])
        return deps

    def format_add_subdirectory(self, subdir):
        return self.format_call("add_subdirectory", [], [self.quote(subdir)])


__all__ = [
    "CMakeContext",
    "CMakeClass",
    "CMakeCmd",
    "CMakeCopy",
    "CMakeModule",
    "CMakeModuleCopy",
    "CMakeMoveDependenciesFromSourceToTarget",
    "CMakeFixMultipleSourceInstancesWithDifferentFlags",
    "CMakeRemoveRedundantDirectoryTargets",
    "CMakeVariable",
    "MergeObjectLibrariesWithSameArguments",
]
