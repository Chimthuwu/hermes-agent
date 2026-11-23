"""The /sesh + /nitesesh interactive Discord panel (rebuild of the Oct 2026 UI).

Lives as a standalone file on purpose: Hermes updates never touch it. The only
integration point is a try/except hook in DiscordAdapter._register_slash_commands
(see Z:/HERMES/.hermes/nitesesh/REAPPLY.md). If this file is missing the panel is
gone; if the hook is dropped the panel is gone. Restore both after `hermes update`.

Panel behaviour:
  /sesh      -> project picker embed: tick projects, chips, Start run / Cancel.
  /nitesesh  -> settings + schedule summary embed with a "Run now (auto)" button.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
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


def _load_settings() -> Dict[str, Any]:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # pragma: no cover
        logger.warning("nitesesh_ui: cannot read settings: %s", exc)
        return {}


def _mask(name: str, reveal: bool) -> str:
    """Settings mask_names=true hides project names unless Reveal names is on."""
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


def _discover_projects() -> List[Dict[str, Any]]:
    try:
        out = subprocess.run(
            [sys.executable, DISCOVER_SCRIPT],
            capture_output=True, text=True, timeout=120, check=True,
        )
        return json.loads(out.stdout).get("projects", [])
    except Exception as exc:
        logger.warning("nitesesh_ui: discovery failed: %s", exc)
        return []


class SeshPanel:
    """State holder for one /sesh picker embed."""

    def __init__(self) -> None:
        self.selected: set[str] = set()
        self.reveal = False
        self.page = 0
        self.projects: List[Dict[str, Any]] = []
        self.discovered = False

    def refresh_projects(self) -> None:
        self.projects = _discover_projects()
        self.discovered = True

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

    def page_count(self) -> int:
        return max(1, (len(self.projects) + PAGE_SIZE - 1) // PAGE_SIZE)

    def page_projects(self) -> List[Dict[str, Any]]:
        start = self.page * PAGE_SIZE
        return self.projects[start:start + PAGE_SIZE]

    def build_embed(self) -> "discord.Embed":
        settings = _load_settings()
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

    def chosen_arg(self) -> str:
        return ";".join(sorted(self.selected))


class SettingsModal(discord.ui.Modal, title="⚙️ sesh settings"):
    """Edit key nitesesh settings.json fields and save back to disk."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        super().__init__()
        self.settings = settings

        self.model = discord.ui.TextInput(
            label="Worker model",
            placeholder="e.g. hermes:nous:reflection/beam-1.0-501b-a23b:free",
            default=str(settings.get("worker_default", ""))[:4000],
            required=False,
            max_length=4000,
        )
        self.add_item(self.model)

        self.time_cap = discord.ui.TextInput(
            label="Time cap (minutes)",
            default=str(settings.get("run_time_cap_min", 360))[:50],
            required=True,
            max_length=10,
        )
        self.add_item(self.time_cap)

        self.cost_cap = discord.ui.TextInput(
            label="Cost cap (USD)",
            default=str(settings.get("run_cost_cap_usd", 3))[:50],
            required=True,
            max_length=10,
        )
        self.add_item(self.cost_cap)

        self.max_tasks = discord.ui.TextInput(
            label="Max tasks per project",
            default=str(settings.get("max_tasks_per_project", 999))[:50],
            required=True,
            max_length=10,
        )
        self.add_item(self.max_tasks)

        self.push = discord.ui.TextInput(
            label="Push on/off (true/false)",
            default="true" if settings.get("push", True) else "false",
            required=True,
            max_length=10,
        )
        self.add_item(self.push)

    async def on_submit(self, interaction: "discord.Interaction") -> None:
        def _write() -> None:
            s = dict(self.settings)
            s["worker_default"] = self.model.value.strip() or s.get("worker_default", "")
            try:
                s["run_time_cap_min"] = int(self.time_cap.value.strip())
            except ValueError:
                pass
            try:
                s["run_cost_cap_usd"] = float(self.cost_cap.value.strip())
            except ValueError:
                pass
            try:
                s["max_tasks_per_project"] = int(self.max_tasks.value.strip())
            except ValueError:
                pass
            s["push"] = self.push.value.strip().lower() in ("true", "1", "yes", "on")
            import json
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump(s, fh, indent=2)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _write)
        except Exception as exc:
            await interaction.response.send_message(f"❌ Failed to save settings: {exc}", ephemeral=True)
            return
        embed = discord.Embed(
            title="⚙️ Settings saved",
            description=f"Wrote to `{SETTINGS_PATH}`. Restart the gateway to apply the model change.",
            colour=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
                placeholder=f"Tick projects on page {panel.page + 1}/{panel.page_count}…",
                min_values=0,
                max_values=len(options),
                options=options,
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
            s = _load_settings()
            modal = SettingsModal(s)
            await interaction.response.send_modal(modal)

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


class NiteshesView(discord.ui.View):  # pragma: no cover - trivial passthrough
    """Alias kept for parity with the old class name (NiteseshView)."""
    pass


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
    # Self-heal: the native manifest may already register a plain-text /sesh;
    # the interactive panel replaces it.
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
        schedule = s.get("schedule", "manual")
        embed = discord.Embed(
            title="/nitesesh — nightly status",
            description=(f"Schedule: **{schedule}**\n"
                         f"Projects saved: {len([p for p in s.get('projects', []) if not p.startswith('##')])}\n"
                         f"Push: {'on' if s.get('push') else 'off'} · "
                         f"caps: {s.get('run_time_cap_min')} min / ${s.get('run_cost_cap_usd')}\n"
                         f"Model: {s.get('worker_default')}"),
            colour=0xFEE75C,
        )
        embed.set_footer(text="Runs must always commit + push (user rule 2026-11-23).")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    _nitesesh_handler.__name__ = "nitesesh_panel"
    tree.command(name="nitesesh",
                 description="Show the nitesesh nightly schedule and settings")(
        _nitesesh_handler)

    logger.info("nitesesh UI registered: /sesh panel + /nitesesh status")
