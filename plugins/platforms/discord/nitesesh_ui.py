"""The /sesh + /nitesesh interactive Discord panel (rebuild of the Oct 2026 UI).

Lives as a standalone file on purpose: Hermes updates never touch it. The only
integration point is a try/except hook in DiscordAdapter._register_slash_commands
(see Z:/HERMES/.hermes/nitesesh/REAPPLY.md). If this file is missing the panel is
gone; if the hook is dropped the panel is gone. Restore both after `hermes update`.

Panel behaviour:
  /sesh      -> project picker embed: tick ALL projects (pagination), Start run / Cancel.
  /nitesesh  -> settings + schedule embed with dropdowns to change them live.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import discord
except ImportError:  # pragma: no cover - adapter only loads this with discord present
    discord = None

logger = logging.getLogger(__name__)

SETTINGS_PATH = "Z:/HERMES/.hermes/nitesesh/settings.json"
DISCOVER_SCRIPT = "Z:/HERMES/.hermes/scripts/discover-todo-projects.py"
RUNNER_SCRIPT = "Z:/HERMES/.hermes/scripts/nitesesh-run.py"
RUNS_DIR = "Z:/HERMES/.hermes/nitesesh/runs"
PAGE_SIZE = 25  # Discord select-menu hard cap

# ---------------------------------------------------------------------------
# project enumeration: list EVERY git repo under roots + extra_projects,
# whether or not it has a todo file (the discovery script drops no-todo repos).
# ---------------------------------------------------------------------------

def _load_settings() -> Dict[str, Any]:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # pragma: no cover
        logger.warning("nitesesh_ui: cannot read settings: %s", exc)
        return {}


def _save_settings(settings: Dict[str, Any]) -> None:
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)


def _open_tasks_for(proj_path: str) -> int:
    """Reuse the discovery script's count when a todo exists; else 0."""
    try:
        out = subprocess.run(
            [sys.executable, DISCOVER_SCRIPT],
            capture_output=True, text=True, timeout=120, check=True,
        )
        data = json.loads(out.stdout)
        for p in data.get("projects", []):
            if p.get("path") == proj_path:
                return p.get("open_tasks", 0)
    except Exception as exc:
        logger.warning("nitesesh_ui: discovery lookup failed: %s", exc)
    return 0


def _all_projects() -> List[Dict[str, Any]]:
    """Enumerate every project folder: roots/* + extra_projects. Show git repos
    even if they have no todo file (discovery script skips those)."""
    cfg = _load_settings()
    roots = cfg.get("roots") or ["Z:/github"]
    extra = cfg.get("extra_projects") or []

    candidates: List[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p).lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(p)

    for r in roots:
        root = Path(r)
        if root.is_dir():
            try:
                for child in root.iterdir():
                    if child.is_dir() and not child.name.startswith((".", "_")):
                        add(child)
            except OSError:
                pass
    for p in extra:
        pp = Path(p)
        if pp.is_dir():
            add(pp)

    out: List[Dict[str, Any]] = []
    for proj in sorted(candidates, key=lambda p: p.name.lower()):
        is_git = (proj / ".git").exists()
        open_n = _open_tasks_for(str(proj)) if is_git else 0
        out.append({
            "name": proj.name,
            "path": str(proj),
            "open_tasks": open_n,
            "is_git": is_git,
        })
    return out


def _mask(name: str, reveal: bool) -> str:
    """mask_names=true hides project names unless Reveal names is on."""
    if reveal or not name:
        return name
    clean = name.replace("·", " ").strip()
    parts = [p for p in clean.split() if p]
    if len(parts) > 1:
        return "_".join(p[:4].upper() for p in parts)
    raw = parts[0] if parts else name
    if len(raw) <= 5:
        return raw.upper()
    return f"{raw[:4].upper()}_{raw[-3:].upper()}"


# ---------------------------------------------------------------------------
# panel state
# ---------------------------------------------------------------------------

class SeshPanel:
    """State holder for one /sesh picker embed."""

    def __init__(self) -> None:
        self.selected: set[str] = set()
        self.reveal = False
        self.page = 0
        self.projects: List[Dict[str, Any]] = []
        self.mode = "projects"  # "projects" | "settings"
        self.discovered = False

    def refresh_projects(self) -> None:
        self.projects = _all_projects()
        self.discovered = True

    def page_count(self) -> int:
        return max(1, (len(self.projects) + PAGE_SIZE - 1) // PAGE_SIZE)

    def page_projects(self) -> List[Dict[str, Any]]:
        start = self.page * PAGE_SIZE
        return self.projects[start:start + PAGE_SIZE]

    def status_fields(self) -> List[tuple]:
        settings = _load_settings()
        push = "on" if settings.get("push", True) else "off"
        return [
            ("Selected", f"{len(self.selected)} project(s)"),
            ("Push", f"{'✅ on' if settings.get('push', True) else '⚠ off'} ({push})"),
            ("Max tasks each", str(settings.get("max_tasks_per_project", 999))),
            ("Model", (settings.get("worker_default") or "default").split(":")[-1]),
            ("Thread output", "off" if settings.get("thread_style", "raw") == "raw" else "on"),
        ]

    def build_embed(self) -> "discord.Embed":
        settings = _load_settings()
        if self.mode == "settings":
            return self._settings_embed(settings)
        return self._projects_embed(settings)

    def _projects_embed(self, settings: Dict[str, Any]) -> "discord.Embed":
        lines: List[str] = []
        for proj in self.page_projects():
            name = _mask(proj["name"], self.reveal)
            n = proj.get("open_tasks", 0)
            warn = "" if proj.get("is_git") else " · no git"
            tick = "✅ " if proj["name"] in self.selected else ""
            lines.append(f"{tick}**{name}** — {n} open{warn}")
        if not lines:
            lines = ["(no projects on this page)"]
        total = len(self.projects)
        embed = discord.Embed(
            title="/sesh — pick projects to work on",
            description="\n".join(lines)[:4000],
            colour=0x5865F2,
        )
        for label, value in self.status_fields():
            embed.add_field(name=label, value=value, inline=True)
        embed.set_footer(text=f"Page {self.page + 1}/{self.page_count()} · "
                              f"{total} project(s) total · "
                              "Tick projects on any page, then Start run")
        return embed

    def _settings_embed(self, settings: Dict[str, Any]) -> "discord.Embed":
        text = (
            f"**push:** {settings.get('push', True)}\n"
            f"**auto_commit_dirty:** {settings.get('auto_commit_dirty', True)}\n"
            f"**use_branches:** {settings.get('use_branches', False)}\n"
            f"**time cap:** {settings.get('run_time_cap_min', 360)} min\n"
            f"**cost cap:** ${settings.get('run_cost_cap_usd', 3)}\n"
            f"**max tasks per project:** {settings.get('max_tasks_per_project', 999)}\n"
            f"**max parallel workers:** {settings.get('max_parallel_workers', 2)}\n"
            f"**worker model:** `{settings.get('worker_default', '(unset)')}`\n"
            f"**settings file:** `{SETTINGS_PATH}`"
        )
        embed = discord.Embed(title="/sesh — settings", description=text, colour=0x57F287)
        embed.set_footer(text="Use the dropdowns below to change a setting · changes save to settings.json")
        return embed

    def chosen_arg(self) -> str:
        return ";".join(sorted(self.selected))


# ---------------------------------------------------------------------------
# settings view: dropdown selects instead of text inputs
# ---------------------------------------------------------------------------

class SettingsView(discord.ui.View):
    def __init__(self, panel: SeshPanel) -> None:
        super().__init__(timeout=None)
        self.panel = panel
        self.settings = _load_settings()
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(ModelSelect(self.panel, self.settings))
        self.add_item(PushSelect(self.panel, self.settings))
        self.add_item(TimeCapSelect(self.panel, self.settings))
        self.add_item(CostCapSelect(self.panel, self.settings))
        self.add_item(MaxTasksSelect(self.panel, self.settings))
        self.add_item(MaxParallelSelect(self.panel, self.settings))
        self.add_item(BackButton(self.panel))

    def refresh(self) -> None:
        self.settings = _load_settings()
        self._build()


class _BaseSelect(discord.ui.Select):
    def __init__(self, panel: SeshPanel, settings: Dict[str, Any],
                 placeholder: str, options: List[discord.SelectOption]) -> None:
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)
        self.panel = panel
        self.settings = settings

    async def _save_and_refresh(self, interaction: "discord.Interaction") -> None:
        _save_settings(self.settings)
        self.panel.refresh()
        self.refresh()
        await interaction.response.edit_message(embed=self.panel.build_embed(), view=self)


class ModelSelect(_BaseSelect):
    def __init__(self, panel: SeshPanel, settings: Dict[str, Any]) -> None:
        presets = list((settings.get("model_presets") or {}).keys())
        current = settings.get("worker_default", "")
        options: List[discord.SelectOption] = []
        for key in presets:
            val = settings["model_presets"][key]
            options.append(discord.SelectOption(
                label=key, value=val, default=(val == current),
                description=f"uses {val.split(':')[-1]}"))
        options.append(discord.SelectOption(
            label="Custom (keep current)", value="__custom__",
            default=(current not in [o.value for o in options if o.value != "__custom__"])))
        super().__init__(panel, settings, "Worker model", options)

    async def callback(self, interaction: "discord.Interaction") -> None:
        val = self.values[0]
        if val != "__custom__":
            self.settings["worker_default"] = val
        await self._save_and_refresh(interaction)


class PushSelect(_BaseSelect):
    def __init__(self, panel: SeshPanel, settings: Dict[str, Any]) -> None:
        cur = bool(settings.get("push", True))
        options = [
            discord.SelectOption(label="On", value="true", default=cur,
                                 description="runs must commit + push"),
            discord.SelectOption(label="Off", value="false", default=not cur),
        ]
        super().__init__(panel, settings, "Push on/off", options)

    async def callback(self, interaction: "discord.Interaction") -> None:
        self.settings["push"] = (self.values[0] == "true")
        await self._save_and_refresh(interaction)


class TimeCapSelect(_BaseSelect):
    def __init__(self, panel: SeshPanel, settings: Dict[str, Any]) -> None:
        cur = settings.get("run_time_cap_min", 360)
        options = [
            discord.SelectOption(label="60 min", value="60", default=cur == 60),
            discord.SelectOption(label="120 min", value="120", default=cur == 120),
            discord.SelectOption(label="180 min", value="180", default=cur == 180),
            discord.SelectOption(label="360 min", value="360", default=cur == 360),
            discord.SelectOption(label="720 min", value="720", default=cur == 720),
        ]
        super().__init__(panel, settings, "Time cap (minutes)", options)

    async def callback(self, interaction: "discord.Interaction") -> None:
        self.settings["run_time_cap_min"] = int(self.values[0])
        await self._save_and_refresh(interaction)


class CostCapSelect(_BaseSelect):
    def __init__(self, panel: SeshPanel, settings: Dict[str, Any]) -> None:
        cur = settings.get("run_cost_cap_usd", 3)
        options = [
            discord.SelectOption(label="$1", value="1", default=cur == 1),
            discord.SelectOption(label="$3", value="3", default=cur == 3),
            discord.SelectOption(label="$5", value="5", default=cur == 5),
            discord.SelectOption(label="$10", value="10", default=cur == 10),
            discord.SelectOption(label="$20", value="20", default=cur == 20),
        ]
        super().__init__(panel, settings, "Cost cap (USD)", options)

    async def callback(self, interaction: "discord.Interaction") -> None:
        self.settings["run_cost_cap_usd"] = float(self.values[0])
        await self._save_and_refresh(interaction)


class MaxTasksSelect(_BaseSelect):
    def __init__(self, panel: SeshPanel, settings: Dict[str, Any]) -> None:
        cur = settings.get("max_tasks_per_project", 999)
        options = [
            discord.SelectOption(label="1", value="1", default=cur == 1),
            discord.SelectOption(label="5", value="5", default=cur == 5),
            discord.SelectOption(label="10", value="10", default=cur == 10),
            discord.SelectOption(label="25", value="25", default=cur == 25),
            discord.SelectOption(label="Unlimited (999)", value="999", default=cur >= 999),
        ]
        super().__init__(panel, settings, "Max tasks per project", options)

    async def callback(self, interaction: "discord.Interaction") -> None:
        self.settings["max_tasks_per_project"] = int(self.values[0])
        await self._save_and_refresh(interaction)


class MaxParallelSelect(_BaseSelect):
    def __init__(self, panel: SeshPanel, settings: Dict[str, Any]) -> None:
        cur = settings.get("max_parallel_workers", 2)
        options = [
            discord.SelectOption(label="1", value="1", default=cur == 1),
            discord.SelectOption(label="2", value="2", default=cur == 2),
            discord.SelectOption(label="4", value="4", default=cur == 4),
            discord.SelectOption(label="8", value="8", default=cur == 8),
        ]
        super().__init__(panel, settings, "Max parallel workers", options)

    async def callback(self, interaction: "discord.Interaction") -> None:
        self.settings["max_parallel_workers"] = int(self.values[0])
        await self._save_and_refresh(interaction)


class BackButton(discord.ui.Button):
    def __init__(self, panel: SeshPanel) -> None:
        super().__init__(label="◀ Back to projects", style=discord.ButtonStyle.secondary)
        self.panel = panel

    async def callback(self, interaction: "discord.Interaction") -> None:
        self.panel.mode = "projects"
        await interaction.response.edit_message(embed=self.panel.build_embed(),
                                                view=_build_view(self.panel))


# ---------------------------------------------------------------------------
# main view builder
# ---------------------------------------------------------------------------

def _build_view(panel: SeshPanel) -> "discord.ui.View":
    assert discord is not None

    class ProjectSelect(discord.ui.Select):
        def __init__(self, page: int) -> None:
            page_projs = panel.page_projects()
            options = [
                discord.SelectOption(
                    label=_mask(p["name"], panel.reveal)[:100],
                    value=p["name"],
                    description=f"{p.get('open_tasks', 0)} open tasks",
                    default=p["name"] in panel.selected,
                )
                for p in page_projs
            ] or [discord.SelectOption(label="No projects on this page", value="none")]
            super().__init__(
                placeholder=f"Tick projects on page {panel.page + 1}/{panel.page_count()}…",
                min_values=0, max_values=len(options), options=options,
            )

        async def callback(self, interaction: "discord.Interaction") -> None:
            panel.selected = (panel.selected - {p["name"] for p in panel.page_projects()}) | (
                set(self.values) - {"none"}
            )
            await interaction.response.edit_message(embed=panel.build_embed(), view=_build_view(panel))

    class SeshView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=None)
            self.add_item(ProjectSelect(panel.page))

        @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=2)
        async def prev_page(self, interaction: "discord.Interaction", _btn: "discord.ui.Button") -> None:
            if panel.page > 0:
                panel.page -= 1
            await interaction.response.edit_message(embed=panel.build_embed(), view=_build_view(panel))

        @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=2)
        async def next_page(self, interaction: "discord.Interaction", _btn: "discord.ui.Button") -> None:
            if panel.page < panel.page_count() - 1:
                panel.page += 1
            await interaction.response.edit_message(embed=panel.build_embed(), view=_build_view(panel))

        @discord.ui.button(label="Refresh list", style=discord.ButtonStyle.secondary, row=2)
        async def refresh(self, interaction: "discord.Interaction", _btn: "discord.ui.Button") -> None:
            await interaction.response.defer()
            await asyncio.get_event_loop().run_in_executor(None, panel.refresh_projects)
            panel.selected &= {p["name"] for p in panel.projects}
            panel.page = 0
            await interaction.edit_original_response(embed=panel.build_embed(), view=_build_view(panel))

        @discord.ui.button(label="Clear", style=discord.ButtonStyle.secondary, row=2)
        async def clear(self, interaction: "discord.Interaction", _btn: "discord.ui.Button") -> None:
            panel.selected.clear()
            await interaction.response.edit_message(embed=panel.build_embed(), view=self)

        @discord.ui.button(label="Reveal names", style=discord.ButtonStyle.secondary, row=2)
        async def reveal(self, interaction: "discord.Interaction", _btn: "discord.ui.Button") -> None:
            panel.reveal = not panel.reveal
            await interaction.response.edit_message(embed=panel.build_embed(), view=self)

        @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary, row=3)
        async def settings_btn(self, interaction: "discord.Interaction", _btn: "discord.ui.Button") -> None:
            panel.mode = "settings"
            await interaction.response.edit_message(embed=panel.build_embed(),
                                                    view=SettingsView(panel))

        @discord.ui.button(label="Start run", style=discord.ButtonStyle.success, row=3)
        async def start(self, interaction: "discord.Interaction", _btn: "discord.ui.Button") -> None:
            if not panel.selected:
                await interaction.response.send_message(
                    "Tick at least one project first (dropdown above).", ephemeral=True)
                return
            settings = _load_settings()
            if not settings.get("push", True):
                await interaction.response.send_message(
                    "⚠ Push is off in settings.json — turn it on before starting "
                    "(runs must commit + push).", ephemeral=True)
                return
            arg = panel.chosen_arg()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            log_path = f"{RUNS_DIR}/ui-{stamp}.log"
            cmd = [sys.executable, RUNNER_SCRIPT, "--projects", arg,
                   "--timeout-min", str(min(settings.get("run_time_cap_min", 360), 360))]
            try:
                os.makedirs(RUNS_DIR, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as logf:
                    subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
            except Exception as exc:
                await interaction.response.send_message(f"❌ Failed to start runner: {exc}",
                                                        ephemeral=True)
                return
            embed = discord.Embed(
                title="🚀 sesh run started",
                description=f"Projects: {arg}\nLog: `{log_path}`",
                colour=0x57F287,
            )
            embed.set_footer(text="Workers commit + push per project; /nitesesh for status.")
            await interaction.response.edit_message(embed=embed, view=None)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=3)
        async def cancel(self, interaction: "discord.Interaction", _btn: "discord.ui.Button") -> None:
            await interaction.response.edit_message(
                embed=discord.Embed(title="/sesh — cancelled", colour=0xED4245), view=None)

    return SeshView()


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def register(adapter: Any, tree: Any) -> None:
    """Register /sesh and /nitesesh on the Discord command tree.

    Called from DiscordAdapter._register_slash_commands. Must never raise.
    """
    if discord is None:
        raise RuntimeError("discord.py not available")

    async def _sesh_handler(interaction: "discord.Interaction") -> None:
        panel = SeshPanel()
        await interaction.response.defer(ephemeral=False, thinking=True)
        await asyncio.get_event_loop().run_in_executor(None, panel.refresh_projects)
        await interaction.edit_original_response(embed=panel.build_embed(),
                                                 view=_build_view(panel))

    _sesh_handler.__name__ = "sesh_panel"
    try:
        if tree.get_command("sesh") is not None:
            tree.remove_command("sesh")
    except Exception:
        pass
    tree.command(name="sesh",
                 description="Pick projects and start sesh workers (interactive panel)")(
        _sesh_handler)

    async def _nitesesh_handler(interaction: "discord.Interaction") -> None:
        s = _load_settings()
        runs_dir = Path(RUNS_DIR)
        recent = []
        if runs_dir.is_dir():
            logs = sorted(runs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            recent = [f"`{p.name}`" for p in logs]
        embed = discord.Embed(
            title="/nitesesh — nightly status",
            description=(
                f"Schedule: **{s.get('schedule', 'manual')}**\n"
                f"Projects saved: {len([p for p in s.get('projects', []) if not str(p).startswith('##')])}\n"
                f"Push: {'on' if s.get('push') else 'off'} · "
                f"caps: {s.get('run_time_cap_min')} min / ${s.get('run_cost_cap_usd')}\n"
                f"Model: `{{s.get('worker_default', '(unset)')}}`\n"
                f"Max parallel: {s.get('max_parallel_workers', 2)} · "
                f"Max tasks/project: {s.get('max_tasks_per_project', 999)}\n"
                + (f"\nRecent run logs: {{', '.join(recent)}}" if recent else "")
            ),
            colour=0xFEE75C,
        )
        embed.set_footer(text="Runs must always commit + push (user rule 2026-11-23).")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    _nitesesh_handler.__name__ = "nitesesh_panel"
    tree.command(name="nitesesh",
                 description="Show the nitesesh nightly schedule and settings")(
        _nitesesh_handler)

    logger.info("nitesesh UI registered: /sesh panel + /nitesesh status")
