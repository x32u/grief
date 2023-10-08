import asyncio
import contextlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Tuple, Union, Iterable, Collection, Optional, Dict, Set, List, cast
from collections import defaultdict

import discord
from redbot.core import commands, Config, version_info as red_version_info
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils import can_user_react_in
from redbot.core.utils.chat_formatting import box, pagify, humanize_list, inline
from redbot.core.utils.menus import start_adding_reactions
from redbot.core.utils.predicates import MessagePredicate, ReactionPredicate

from . import errors
from .checks import do_install_agreement
from .converters import InstalledCog
from .installable import InstallableType, Installable, InstalledModule
from .log import log
from .repo_manager import RepoManager, Repo

_ = Translator("Downloader", __file__)


DEPRECATION_NOTICE = _(
    "\n**WARNING:** The following repos are using shared libraries"
    " which are marked for removal in the future: {repo_list}.\n"
    " You should inform maintainers of these repos about this message."
)


@cog_i18n(_)
class Downloader(commands.Cog):
    """Install community cogs made by Cog Creators.

    Community cogs, also called third party cogs, are not included
    in the default Red install.

    Community cogs come in repositories. Repos are a group of cogs
    you can install. You always need to add the creator's repository
    using the `[p]repo` command before you can install one or more
    cogs from the creator.
    """

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot

        self.config = Config.get_conf(self, identifier=998240343, force_registration=True)

        self.config.register_global(schema_version=0, installed_cogs={}, installed_libraries={})

        self.already_agreed = False

        self.LIB_PATH = cog_data_path(self) / "lib"
        self.SHAREDLIB_PATH = self.LIB_PATH / "cog_shared"
        self.SHAREDLIB_INIT = self.SHAREDLIB_PATH / "__init__.py"

        self._create_lib_folder()

        self._repo_manager = RepoManager()
        self._ready = asyncio.Event()
        self._init_task = None
        self._ready_raised = False

    def _create_lib_folder(self, *, remove_first: bool = False) -> None:
        if remove_first:
            shutil.rmtree(str(self.LIB_PATH))
        self.SHAREDLIB_PATH.mkdir(parents=True, exist_ok=True)
        if not self.SHAREDLIB_INIT.exists():
            with self.SHAREDLIB_INIT.open(mode="w", encoding="utf-8") as _:
                pass

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        if not self._ready.is_set():
            async with ctx.typing():
                await self._ready.wait()
        if self._ready_raised:
            await ctx.send(
                "There was an error during Downloader's initialization."
                " Check logs for more information."
            )
            raise commands.CheckFailure()

    def cog_unload(self):
        if self._init_task is not None:
            self._init_task.cancel()

    def create_init_task(self):
        def _done_callback(task: asyncio.Task) -> None:
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                pass
            else:
                if exc is None:
                    return
                log.error(
                    "An unexpected error occurred during Downloader's initialization.",
                    exc_info=exc,
                )
            self._ready_raised = True
            self._ready.set()

        self._init_task = asyncio.create_task(self.initialize())
        self._init_task.add_done_callback(_done_callback)

    async def initialize(self) -> None:
        await self._repo_manager.initialize()
        await self._maybe_update_config()
        self._ready.set()

    async def _maybe_update_config(self) -> None:
        schema_version = await self.config.schema_version()

        if schema_version == 0:
            await self._schema_0_to_1()
            schema_version += 1
            await self.config.schema_version.set(schema_version)

    async def _schema_0_to_1(self):
        """
        This contains migration to allow saving state
        of both installed cogs and shared libraries.
        """
        old_conf = await self.config.get_raw("installed", default=[])
        if not old_conf:
            return
        async with self.config.installed_cogs() as new_cog_conf:
            for cog_json in old_conf:
                repo_name = cog_json["repo_name"]
                module_name = cog_json["cog_name"]
                if repo_name not in new_cog_conf:
                    new_cog_conf[repo_name] = {}
                new_cog_conf[repo_name][module_name] = {
                    "repo_name": repo_name,
                    "module_name": module_name,
                    "commit": "",
                    "pinned": False,
                }
        await self.config.clear_raw("installed")
        # no reliable way to get installed libraries (i.a. missing repo name)
        # but it only helps `[p]cog update` run faster so it's not an issue

    async def cog_install_path(self) -> Path:
        """Get the current cog install path.

        Returns
        -------
        pathlib.Path
            The default cog install path.

        """
        return await self.bot._cog_mgr.install_path()

    async def installed_cogs(self) -> Tuple[InstalledModule, ...]:
        """Get info on installed cogs.

        Returns
        -------
        `tuple` of `InstalledModule`
            All installed cogs.

        """
        installed = await self.config.installed_cogs()
        # noinspection PyTypeChecker
        return tuple(
            InstalledModule.from_json(cog_json, self._repo_manager)
            for repo_json in installed.values()
            for cog_json in repo_json.values()
        )

    async def installed_libraries(self) -> Tuple[InstalledModule, ...]:
        """Get info on installed shared libraries.

        Returns
        -------
        `tuple` of `InstalledModule`
            All installed shared libraries.

        """
        installed = await self.config.installed_libraries()
        # noinspection PyTypeChecker
        return tuple(
            InstalledModule.from_json(lib_json, self._repo_manager)
            for repo_json in installed.values()
            for lib_json in repo_json.values()
        )

    async def installed_modules(self) -> Tuple[InstalledModule, ...]:
        """Get info on installed cogs and shared libraries.

        Returns
        -------
        `tuple` of `InstalledModule`
            All installed cogs and shared libraries.

        """
        return await self.installed_cogs() + await self.installed_libraries()

    async def _save_to_installed(self, modules: Iterable[InstalledModule]) -> None:
        """Mark modules as installed or updates their json in Config.

        Parameters
        ----------
        modules : `list` of `InstalledModule`
            The modules to check off.

        """
        async with self.config.all() as global_data:
            installed_cogs = global_data["installed_cogs"]
            installed_libraries = global_data["installed_libraries"]
            for module in modules:
                if module.type == InstallableType.COG:
                    installed = installed_cogs
                elif module.type == InstallableType.SHARED_LIBRARY:
                    installed = installed_libraries
                else:
                    continue
                module_json = module.to_json()
                repo_json = installed.setdefault(module.repo_name, {})
                repo_json[module.name] = module_json

    async def _remove_from_installed(self, modules: Iterable[InstalledModule]) -> None:
        """Remove modules from the saved list
        of installed modules (corresponding to type of module).

        Parameters
        ----------
        modules : `list` of `InstalledModule`
            The modules to remove.

        """
        async with self.config.all() as global_data:
            installed_cogs = global_data["installed_cogs"]
            installed_libraries = global_data["installed_libraries"]
            for module in modules:
                if module.type == InstallableType.COG:
                    installed = installed_cogs
                elif module.type == InstallableType.SHARED_LIBRARY:
                    installed = installed_libraries
                else:
                    continue
                with contextlib.suppress(KeyError):
                    installed[module._json_repo_name].pop(module.name)

    async def _shared_lib_load_check(self, cog_name: str) -> Optional[Repo]:
        is_installed, cog = await self.is_installed(cog_name)
        # it's not gonna be None when `is_installed` is True
        # if we'll use typing_extensions in future, `Literal` can solve this
        cog = cast(InstalledModule, cog)
        if is_installed and cog.repo is not None and cog.repo.available_libraries:
            return cog.repo
        return None

    async def _available_updates(
        self, cogs: Iterable[InstalledModule]
    ) -> Tuple[Tuple[Installable, ...], Tuple[Installable, ...]]:
        """
        Get cogs and libraries which can be updated.

        Parameters
        ----------
        cogs : `list` of `InstalledModule`
            List of cogs, which should be checked against the updates.

        Returns
        -------
        tuple
            2-tuple of cogs and libraries which can be updated.

        """
        repos = {cog.repo for cog in cogs if cog.repo is not None}
        installed_libraries = await self.installed_libraries()

        modules: Set[InstalledModule] = set()
        cogs_to_update: Set[Installable] = set()
        libraries_to_update: Set[Installable] = set()
        # split libraries and cogs into 2 categories:
        # 1. `cogs_to_update`, `libraries_to_update` - module needs update, skip diffs
        # 2. `modules` - module MAY need update, check diffs
        for repo in repos:
            for lib in repo.available_libraries:
                try:
                    index = installed_libraries.index(lib)
                except ValueError:
                    libraries_to_update.add(lib)
                else:
                    modules.add(installed_libraries[index])
        for cog in cogs:
            if cog.repo is None:
                # cog had its repo removed, can't check for updates
                continue
            if cog.commit:
                modules.add(cog)
                continue
            # marking cog for update if there's no commit data saved (back-compat, see GH-2571)
            last_cog_occurrence = await cog.repo.get_last_module_occurrence(cog.name)
            if last_cog_occurrence is not None and not last_cog_occurrence.disabled:
                cogs_to_update.add(last_cog_occurrence)

        # Reduces diff requests to a single dict with no repeats
        hashes: Dict[Tuple[Repo, str], Set[InstalledModule]] = defaultdict(set)
        for module in modules:
            module.repo = cast(Repo, module.repo)
            if module.repo.commit != module.commit:
                try:
                    should_add = await module.repo.is_ancestor(module.commit, module.repo.commit)
                except errors.UnknownRevision:
                    # marking module for update if the saved commit data is invalid
                    last_module_occurrence = await module.repo.get_last_module_occurrence(
                        module.name
                    )
                    if last_module_occurrence is not None and not last_module_occurrence.disabled:
                        if last_module_occurrence.type == InstallableType.COG:
                            cogs_to_update.add(last_module_occurrence)
                        elif last_module_occurrence.type == InstallableType.SHARED_LIBRARY:
                            libraries_to_update.add(last_module_occurrence)
                else:
                    if should_add:
                        hashes[(module.repo, module.commit)].add(module)

        update_commits = []
        for (repo, old_hash), modules_to_check in hashes.items():
            modified = await repo.get_modified_modules(old_hash, repo.commit)
            for module in modules_to_check:
                try:
                    index = modified.index(module)
                except ValueError:
                    # module wasn't modified - we just need to update its commit
                    module.commit = repo.commit
                    update_commits.append(module)
                else:
                    modified_module = modified[index]
                    if modified_module.type == InstallableType.COG:
                        if not modified_module.disabled:
                            cogs_to_update.add(modified_module)
                    elif modified_module.type == InstallableType.SHARED_LIBRARY:
                        libraries_to_update.add(modified_module)

        await self._save_to_installed(update_commits)

        return (tuple(cogs_to_update), tuple(libraries_to_update))

    async def _install_cogs(
        self, cogs: Iterable[Installable]
    ) -> Tuple[Tuple[InstalledModule, ...], Tuple[Installable, ...]]:
        """Installs a list of cogs.

        Parameters
        ----------
        cogs : `list` of `Installable`
            Cogs to install. ``repo`` property of those objects can't be `None`
        Returns
        -------
        tuple
            2-tuple of installed and failed cogs.
        """
        repos: Dict[str, Tuple[Repo, Dict[str, List[Installable]]]] = {}
        for cog in cogs:
            try:
                repo_by_commit = repos[cog.repo_name]
            except KeyError:
                cog.repo = cast(Repo, cog.repo)  # docstring specifies this already
                repo_by_commit = repos[cog.repo_name] = (cog.repo, defaultdict(list))
            cogs_by_commit = repo_by_commit[1]
            cogs_by_commit[cog.commit].append(cog)
        installed = []
        failed = []
        for repo, cogs_by_commit in repos.values():
            exit_to_commit = repo.commit
            for commit, cogs_to_install in cogs_by_commit.items():
                await repo.checkout(commit)
                for cog in cogs_to_install:
                    if await cog.copy_to(await self.cog_install_path()):
                        installed.append(InstalledModule.from_installable(cog))
                    else:
                        failed.append(cog)
            await repo.checkout(exit_to_commit)

        # noinspection PyTypeChecker
        return (tuple(installed), tuple(failed))

    async def _reinstall_libraries(
        self, libraries: Iterable[Installable]
    ) -> Tuple[Tuple[InstalledModule, ...], Tuple[Installable, ...]]:
        """Installs a list of shared libraries, used when updating.

        Parameters
        ----------
        libraries : `list` of `Installable`
            Libraries to reinstall. ``repo`` property of those objects can't be `None`
        Returns
        -------
        tuple
            2-tuple of installed and failed libraries.
        """
        repos: Dict[str, Tuple[Repo, Dict[str, Set[Installable]]]] = {}
        for lib in libraries:
            try:
                repo_by_commit = repos[lib.repo_name]
            except KeyError:
                lib.repo = cast(Repo, lib.repo)  # docstring specifies this already
                repo_by_commit = repos[lib.repo_name] = (lib.repo, defaultdict(set))
            libs_by_commit = repo_by_commit[1]
            libs_by_commit[lib.commit].add(lib)

        all_installed: List[InstalledModule] = []
        all_failed: List[Installable] = []
        for repo, libs_by_commit in repos.values():
            exit_to_commit = repo.commit
            for commit, libs in libs_by_commit.items():
                await repo.checkout(commit)
                installed, failed = await repo.install_libraries(
                    target_dir=self.SHAREDLIB_PATH, req_target_dir=self.LIB_PATH, libraries=libs
                )
                all_installed += installed
                all_failed += failed
            await repo.checkout(exit_to_commit)

        # noinspection PyTypeChecker
        return (tuple(all_installed), tuple(all_failed))

    async def _install_requirements(self, cogs: Iterable[Installable]) -> Tuple[str, ...]:
        """
        Installs requirements for given cogs.

        Parameters
        ----------
        cogs : `list` of `Installable`
            Cogs whose requirements should be installed.
        Returns
        -------
        tuple
            Tuple of failed requirements.
        """

        # Reduces requirements to a single list with no repeats
        requirements = {requirement for cog in cogs for requirement in cog.requirements}
        repos: List[Tuple[Repo, List[str]]] = [(repo, []) for repo in self._repo_manager.repos]

        # This for loop distributes the requirements across all repos
        # which will allow us to concurrently install requirements
        for i, req in enumerate(requirements):
            repo_index = i % len(repos)
            repos[repo_index][1].append(req)

        has_reqs = list(filter(lambda item: len(item[1]) > 0, repos))

        failed_reqs = []
        for repo, reqs in has_reqs:
            for req in reqs:
                if not await repo.install_raw_requirements([req], self.LIB_PATH):
                    failed_reqs.append(req)
        return tuple(failed_reqs)

    @staticmethod
    async def _delete_cog(target: Path) -> None:
        """
        Removes an (installed) cog.
        :param target: Path pointing to an existing file or directory
        :return:
        """
        if not target.exists():
            return

        if target.is_dir():
            shutil.rmtree(str(target))
        elif target.is_file():
            os.remove(str(target))

    @staticmethod
    async def send_pagified(target: discord.abc.Messageable, content: str) -> None:
        for page in pagify(content):
            await target.send(page)

    @commands.command(require_var_positional=True)
    @commands.is_owner()
    async def pipinstall(self, ctx: commands.Context, *deps: str) -> None:
        """
        Install a group of dependencies using pip.

        Examples:
        - `[p]pipinstall bs4`
        - `[p]pipinstall py-cpuinfo psutil`

        Improper usage of this command can break your bot, be careful.

        **Arguments**

        - `<deps...>` The package or packages you wish to install.
        """
        repo = Repo("", "", "", "", Path.cwd())
        async with ctx.typing():
            success = await repo.install_raw_requirements(deps, self.LIB_PATH)

        if success:
            await ctx.send(_("Libraries installed.") if len(deps) > 1 else _("Library installed."))
        else:
            await ctx.send(
                _(
                    "Some libraries failed to install. Please check"
                    " your logs for a complete list."
                )
                if len(deps) > 1
                else _(
                    "The library failed to install. Please check your logs for a complete list."
                )
            )

    @commands.group()
    @commands.is_owner()
    async def cog(self, ctx: commands.Context) -> None:
        """Base command for cog installation management commands."""
        pass

    @cog.command(name="reinstallreqs", hidden=True)
    async def _cog_reinstallreqs(self, ctx: commands.Context) -> None:
        """
        This command should not be used unless Red specifically asks for it.

        This command will reinstall cog requirements and shared libraries for all installed cogs.

        Red might ask the owner to use this when it clears contents of the lib folder
        because of change in minor version of Python.
        """
        async with ctx.typing():
            self._create_lib_folder(remove_first=True)
            installed_cogs = await self.installed_cogs()
            cogs = []
            repos = set()
            for cog in installed_cogs:
                if cog.repo is None:
                    continue
                repos.add(cog.repo)
                cogs.append(cog)
            failed_reqs = await self._install_requirements(cogs)
            all_installed_libs: List[InstalledModule] = []
            all_failed_libs: List[Installable] = []
            for repo in repos:
                installed_libs, failed_libs = await repo.install_libraries(
                    target_dir=self.SHAREDLIB_PATH, req_target_dir=self.LIB_PATH
                )
                all_installed_libs += installed_libs
                all_failed_libs += failed_libs
        message = ""
        if failed_reqs:
            message += (
                _("Failed to install requirements: ")
                if len(failed_reqs) > 1
                else _("Failed to install the requirement: ")
            ) + humanize_list(tuple(map(inline, failed_reqs)))
        if all_failed_libs:
            libnames = [lib.name for lib in failed_libs]
            message += (
                _("\nFailed to install shared libraries: ")
                if len(all_failed_libs) > 1
                else _("\nFailed to install shared library: ")
            ) + humanize_list(tuple(map(inline, libnames)))
        if message:
            await self.send_pagified(
                ctx,
                _(
                    "Cog requirements and shared libraries for all installed cogs"
                    " have been reinstalled but there were some errors:\n"
                )
                + message,
            )
        else:
            await ctx.send(
                _(
                    "Cog requirements and shared libraries"
                    " for all installed cogs have been reinstalled."
                )
            )

    async def is_installed(
        self, cog_name: str
    ) -> Union[Tuple[bool, InstalledModule], Tuple[bool, None]]:
        """Check to see if a cog has been installed through Downloader.

        Parameters
        ----------
        cog_name : str
            The name of the cog to check for.

        Returns
        -------
        `tuple` of (`bool`, `InstalledModule`)
            :code:`(True, InstalledModule)` if the cog is installed, else
            :code:`(False, None)`.

        """
        for installed_cog in await self.installed_cogs():
            if installed_cog.name == cog_name:
                return True, installed_cog
        return False, None

    async def _filter_incorrect_cogs_by_names(
        self, repo: Repo, cog_names: Iterable[str]
    ) -> Tuple[Tuple[Installable, ...], str]:
        """Filter out incorrect cogs from list.

        Parameters
        ----------
        repo : `Repo`
            Repo which should be searched for `cog_names`
        cog_names : `list` of `str`
            Cog names to search for in repo.
        Returns
        -------
        tuple
            2-tuple of cogs to install and error message for incorrect cogs.
        """
        installed_cogs = await self.installed_cogs()
        cogs: List[Installable] = []
        unavailable_cogs: List[str] = []
        already_installed: List[str] = []
        name_already_used: List[str] = []

        for cog_name in cog_names:
            cog: Optional[Installable] = discord.utils.get(repo.available_cogs, name=cog_name)
            if cog is None:
                unavailable_cogs.append(inline(cog_name))
                continue
            if cog in installed_cogs:
                already_installed.append(inline(cog_name))
                continue
            if discord.utils.get(installed_cogs, name=cog.name):
                name_already_used.append(inline(cog_name))
                continue
            cogs.append(cog)

        message = ""

        if unavailable_cogs:
            message = (
                _("\nCouldn't find these cogs in {repo.name}: ")
                if len(unavailable_cogs) > 1
                else _("\nCouldn't find this cog in {repo.name}: ")
            ).format(repo=repo) + humanize_list(unavailable_cogs)
        if already_installed:
            message += (
                _("\nThese cogs were already installed: ")
                if len(already_installed) > 1
                else _("\nThis cog was already installed: ")
            ) + humanize_list(already_installed)
        if name_already_used:
            message += (
                _("\nSome cogs with these names are already installed from different repos: ")
                if len(name_already_used) > 1
                else _("\nCog with this name is already installed from a different repo: ")
            ) + humanize_list(name_already_used)
        correct_cogs, add_to_message = self._filter_incorrect_cogs(cogs)
        if add_to_message:
            return correct_cogs, f"{message}{add_to_message}"
        return correct_cogs, message

    def _filter_incorrect_cogs(
        self, cogs: Iterable[Installable]
    ) -> Tuple[Tuple[Installable, ...], str]:
        correct_cogs: List[Installable] = []
        outdated_python_version: List[str] = []
        outdated_bot_version: List[str] = []
        for cog in cogs:
            if cog.min_python_version > sys.version_info:
                outdated_python_version.append(
                    inline(cog.name)
                    + _(" (Minimum: {min_version})").format(
                        min_version=".".join([str(n) for n in cog.min_python_version])
                    )
                )
                continue
            ignore_max = cog.min_bot_version > cog.max_bot_version
            if (
                cog.min_bot_version > red_version_info
                or not ignore_max
                and cog.max_bot_version < red_version_info
            ):
                outdated_bot_version.append(
                    inline(cog.name)
                    + _(" (Minimum: {min_version}").format(min_version=cog.min_bot_version)
                    + (
                        ""
                        if ignore_max
                        else _(", at most: {max_version}").format(max_version=cog.max_bot_version)
                    )
                    + ")"
                )
                continue
            correct_cogs.append(cog)
        message = ""
        if outdated_python_version:
            message += (
                _("\nThese cogs require higher python version than you have: ")
                if len(outdated_python_version)
                else _("\nThis cog requires higher python version than you have: ")
            ) + humanize_list(outdated_python_version)
        if outdated_bot_version:
            message += (
                _(
                    "\nThese cogs require different Red version"
                    " than you currently have ({current_version}): "
                )
                if len(outdated_bot_version) > 1
                else _(
                    "\nThis cog requires different Red version than you currently "
                    "have ({current_version}): "
                )
            ).format(current_version=red_version_info) + humanize_list(outdated_bot_version)

        return tuple(correct_cogs), message

    async def _get_cogs_to_check(
        self,
        *,
        repos: Optional[Iterable[Repo]] = None,
        cogs: Optional[Iterable[InstalledModule]] = None,
        update_repos: bool = True,
    ) -> Tuple[Set[InstalledModule], List[str]]:
        failed = []
        if not (cogs or repos):
            if update_repos:
                __, failed = await self._repo_manager.update_repos()

            cogs_to_check = {
                cog
                for cog in await self.installed_cogs()
                if cog.repo is not None and cog.repo.name not in failed
            }
        else:
            # this is enough to be sure that `cogs` is not None (based on if above)
            if not repos:
                cogs = cast(Iterable[InstalledModule], cogs)
                repos = {cog.repo for cog in cogs if cog.repo is not None}

            if update_repos:
                __, failed = await self._repo_manager.update_repos(repos)

            if failed:
                # remove failed repos
                repos = {repo for repo in repos if repo.name not in failed}

            if cogs:
                cogs_to_check = {cog for cog in cogs if cog.repo is not None and cog.repo in repos}
            else:
                cogs_to_check = {
                    cog
                    for cog in await self.installed_cogs()
                    if cog.repo is not None and cog.repo in repos
                }

        return (cogs_to_check, failed)

    async def _update_cogs_and_libs(
        self,
        ctx: commands.Context,
        cogs_to_update: Iterable[Installable],
        libs_to_update: Iterable[Installable],
        current_cog_versions: Iterable[InstalledModule],
    ) -> Tuple[Set[str], str]:
        current_cog_versions_map = {cog.name: cog for cog in current_cog_versions}
        failed_reqs = await self._install_requirements(cogs_to_update)
        if failed_reqs:
            return (
                set(),
                (
                    _("Failed to install requirements: ")
                    if len(failed_reqs) > 1
                    else _("Failed to install the requirement: ")
                )
                + humanize_list(tuple(map(inline, failed_reqs))),
            )
        installed_cogs, failed_cogs = await self._install_cogs(cogs_to_update)
        installed_libs, failed_libs = await self._reinstall_libraries(libs_to_update)
        await self._save_to_installed(installed_cogs + installed_libs)
        message = _("Cog update completed successfully.")

        updated_cognames: Set[str] = set()
        if installed_cogs:
            updated_cognames = set()
            cogs_with_changed_eud_statement = set()
            for cog in installed_cogs:
                updated_cognames.add(cog.name)
                current_eud_statement = current_cog_versions_map[cog.name].end_user_data_statement
                if current_eud_statement != cog.end_user_data_statement:
                    cogs_with_changed_eud_statement.add(cog.name)
            message += _("\nUpdated: ") + humanize_list(tuple(map(inline, updated_cognames)))
            if cogs_with_changed_eud_statement:
                if len(cogs_with_changed_eud_statement) > 1:
                    message += (
                        _("\nEnd user data statements of these cogs have changed: ")
                        + humanize_list(tuple(map(inline, cogs_with_changed_eud_statement)))
                        + _("\nYou can use {command} to see the updated statements.\n").format(
                            command=inline(f"{ctx.clean_prefix}cog info <repo> <cog>")
                        )
                    )
                else:
                    message += (
                        _("\nEnd user data statement of this cog has changed:")
                        + inline(next(iter(cogs_with_changed_eud_statement)))
                        + _("\nYou can use {command} to see the updated statement.\n").format(
                            command=inline(f"{ctx.clean_prefix}cog info <repo> <cog>")
                        )
                    )
            # If the bot has any slash commands enabled, warn them to sync
            enabled_slash = await self.bot.list_enabled_app_commands()
            if any(enabled_slash.values()):
                message += _(
                    "\nYou may need to resync your slash commands with `{prefix}slash sync`."
                ).format(prefix=ctx.prefix)
        if failed_cogs:
            cognames = [cog.name for cog in failed_cogs]
            message += (
                _("\nFailed to update cogs: ")
                if len(failed_cogs) > 1
                else _("\nFailed to update cog: ")
            ) + humanize_list(tuple(map(inline, cognames)))
        if not cogs_to_update:
            message = _("No cogs were updated.")
        if installed_libs:
            message += (
                _(
                    "\nSome shared libraries were updated, you should restart the bot "
                    "to bring the changes into effect."
                )
                if len(installed_libs) > 1
                else _(
                    "\nA shared library was updated, you should restart the "
                    "bot to bring the changes into effect."
                )
            )
        if failed_libs:
            libnames = [lib.name for lib in failed_libs]
            message += (
                _("\nFailed to install shared libraries: ")
                if len(failed_cogs) > 1
                else _("\nFailed to install shared library: ")
            ) + humanize_list(tuple(map(inline, libnames)))
        return (updated_cognames, message)

    async def _ask_for_cog_reload(self, ctx: commands.Context, updated_cognames: Set[str]) -> None:
        updated_cognames &= ctx.bot.extensions.keys()  # only reload loaded cogs
        if not updated_cognames:
            await ctx.send(_("None of the updated cogs were previously loaded. Update complete."))
            return

        if not ctx.assume_yes:
            message = (
                _("Would you like to reload the updated cogs?")
                if len(updated_cognames) > 1
                else _("Would you like to reload the updated cog?")
            )
            can_react = can_user_react_in(ctx.me, ctx.channel)
            if not can_react:
                message += " (yes/no)"
            query: discord.Message = await ctx.send(message)
            if can_react:
                # noinspection PyAsyncCall
                start_adding_reactions(query, ReactionPredicate.YES_OR_NO_EMOJIS)
                pred = ReactionPredicate.yes_or_no(query, ctx.author)
                event = "reaction_add"
            else:
                pred = MessagePredicate.yes_or_no(ctx)
                event = "message"
            try:
                await ctx.bot.wait_for(event, check=pred, timeout=30)
            except asyncio.TimeoutError:
                with contextlib.suppress(discord.NotFound):
                    await query.delete()
                return

            if not pred.result:
                if can_react:
                    with contextlib.suppress(discord.NotFound):
                        await query.delete()
                else:
                    await ctx.send(_("OK then."))
                return
            else:
                if can_react:
                    with contextlib.suppress(discord.Forbidden):
                        await query.clear_reactions()

        await ctx.invoke(ctx.bot.get_cog("Core").reload, *updated_cognames)

    def cog_name_from_instance(self, instance: object) -> str:
        """Determines the cog name that Downloader knows from the cog instance.

        Probably.

        Parameters
        ----------
        instance : object
            The cog instance.

        Returns
        -------
        str
            The name of the cog according to Downloader..

        """
        splitted = instance.__module__.split(".")
        return splitted[0]

    @staticmethod
    def format_failed_repos(failed: Collection[str]) -> str:
        """Format collection of ``Repo.name``'s into failed message.

        Parameters
        ----------
        failed : Collection
            Collection of ``Repo.name``

        Returns
        -------
        str
            formatted message
        """

        message = (
            _("Failed to update the following repositories:")
            if len(failed) > 1
            else _("Failed to update the following repository:")
        )
        message += " " + humanize_list(tuple(map(inline, failed))) + "\n"
        message += _(
            "The repository's branch might have been removed or"
            " the repository is no longer accessible at set url."
            " See logs for more information."
        )
        return message