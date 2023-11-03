import argparse
import dataclasses
import json
import os
import pathlib
import subprocess


@dataclasses.dataclass(frozen=True, kw_only=True)
class Charm:
    github_repository: str
    ref: str
    relative_path_to_charmcraft_yaml: str

    @property
    def _repository_directory(self) -> pathlib.Path:
        return pathlib.Path("repos", self.github_repository)

    def checkout_repository(self):
        try:
            self._repository_directory.mkdir(parents=True)
        except FileExistsError:
            commands = [
                ["git", "fetch", "origin", self.ref],
                ["git", "checkout", "FETCH_HEAD"],
            ]
        else:
            commands = [
                ["git", "init"],
                [
                    "git",
                    "sparse-checkout",
                    "set",
                    "--sparse-index",
                    self.relative_path_to_charmcraft_yaml,
                ],
                [
                    "git",
                    "remote",
                    "add",
                    "--fetch",
                    "origin",
                    f"https://github.com/{self.github_repository}.git",
                ],
                ["git", "fetch", "origin", self.ref],
                ["git", "checkout", "FETCH_HEAD"],
            ]
        for command in commands:
            subprocess.run(command, cwd=self._repository_directory, check=True)

    @property
    def directory(self) -> pathlib.Path:
        return self._repository_directory / self.relative_path_to_charmcraft_yaml


@dataclasses.dataclass(frozen=True, kw_only=True)
class Dependency:
    name: str
    version: str


def main():
    pip_cache = pathlib.Path("~/charmcraftcache-hub-ci/build/").expanduser()
    pip_cache.mkdir(parents=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("charms_file")
    args = parser.parse_args()
    with open(args.charms_file, "r") as file:
        charms = [Charm(**charm) for charm in json.load(file)]
    dependencies_by_charm: dict[Charm, set[Dependency]] = {}
    for charm in charms:
        charm.checkout_repository()
        assert (charm.directory / "poetry.lock").exists()
        subprocess.run(
            [
                "poetry",
                "export",
                # Ignore other dependency groups (e.g. unit test, lint, etc.)
                "--only",
                "main",
                "--output",
                "requirements.txt",
            ],
            cwd=charm.directory,
            check=True,
        )

        env = os.environ
        env["XDG_CACHE_HOME"] = str(pip_cache)
        subprocess.run(
            [
                "pip",
                "install",
                "-r",
                "requirements.txt",
                # Build wheels from source
                "--no-binary",
                ":all:",
                # Cache will still be hit if exact version of wheel available
                # `--ignore-installed` needed:
                # - to ignore non-exact versions
                # - to include all dependencies in report
                "--ignore-installed",
                "--report",
                "report.json",
            ],
            cwd=charm.directory,
            check=True,
            env=env,
        )
        with open(charm.directory / "report.json", "r") as file:
            report = json.load(file)
        dependencies_by_charm[charm] = {
            Dependency(
                name=dependency["metadata"]["name"],
                version=dependency["metadata"]["version"],
            )
            for dependency in report["install"]
        }
    serializable_dependencies = {}
    for charm, dependencies in dependencies_by_charm.items():
        serializable_dependencies[str(dataclasses.asdict(charm))] = [
            dataclasses.asdict(dependency) for dependency in dependencies
        ]
    release_artifacts = pathlib.Path("~/charmcraftcache-hub-ci/release/")
    release_artifacts.mkdir(parents=True)
    with open(release_artifacts / "dependencies_by_charm.json", "w") as file:
        json.dump(serializable_dependencies, file, indent=2)
    # Rename .whl files to include relative path from `~/charmcraftcache-hub-ci/build/pip/wheels/`
    for wheel in (pip_cache / "pip/wheels/").glob("**/*.whl"):
        # Example:
        # `~/charmcraftcache-hub-ci/build/pip/wheels/a6/bb/99/9eae10e99b02cc1daa8f370d631ae22d9a1378c33d04b598b6/setuptools-68.2.2-py3-none-any.whl`
        # is moved to
        # `~/charmcraftcache-hub-ci/release/setuptools-68.2.2-py3-none-any.whl.a6_bb_99_9eae10e99b02cc1daa8f370d631ae22d9a1378c33d04b598b6.charmcraftcachehub`
        wheel.rename(
            pathlib.PurePath(
                release_artifacts,
                f'{wheel.name}.{str(wheel.parent).replace("/", "_")}.charmcraftcachehub',
            )
        )
