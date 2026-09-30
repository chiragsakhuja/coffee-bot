"""Tools Claude can call. Built per turn so they can talk back to the right Telegram chat."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Literal, Protocol
from zoneinfo import ZoneInfo

from anthropic import beta_async_tool
from pydantic import ValidationError

from .models import MAX_COFFEES, BrewMethod, Coffee, Menu, now_utc
from .render import Renderer, RenderResult
from .sandbox import MAX_FILE_BYTES, Sandbox, run_command
from .store import MenuStore, atomic_write_bytes

log = logging.getLogger(__name__)

Container = Literal["black", "white", "green", "blue"]
BrewStatus = Literal["dialing_in", "dialed", "trying"]


class ChatIO(Protocol):
    """What the tools need from the chat front-end (implemented by bot.py)."""

    async def send_photo(self, png: bytes, caption: str | None = None) -> None: ...
    async def request_approval(self, command: str, reason: str) -> bool: ...


@dataclass
class Services:
    store: MenuStore
    renderer: Renderer
    sandbox: Sandbox
    tz: ZoneInfo
    menu_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    async def render(self, menu: Menu | None = None) -> RenderResult:
        return await self.renderer.render(menu or self.store.load(), self.today())


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"roast_date must be YYYY-MM-DD, got {value!r}")


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def build_tools(svc: Services, chat: ChatIO) -> list:
    async def mutate(change: Callable[[Menu], str]) -> str:
        """Load → apply → validate+save → re-render. Returns the change's message plus render status."""
        async with svc.menu_lock:
            menu = svc.store.load()
            try:
                message = change(menu)
                svc.store.save(menu)
            except ValidationError as exc:
                raise ValueError(_validation_message(exc)) from None
            saved = svc.store.load()
        try:
            await svc.render(saved)
            return f"{message} The display image was re-rendered."
        except Exception as exc:  # template bugs shouldn't lose the data change
            log.exception("render failed")
            return f"{message} WARNING: saved, but rendering failed: {exc}"

    @beta_async_tool
    async def get_menu() -> str:
        """Return the current menu as JSON, including every coffee's id, details, and brew methods."""
        return svc.store.load().model_dump_json(indent=2)

    @beta_async_tool
    async def add_coffee(
        roaster: str,
        name: str,
        roast_date: str,
        container: Container,
        tasting_notes: list[str],
        origin: str | None = None,
        process: str | None = None,
        roast_level: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Add a new coffee to the menu. Fails if the menu already has the maximum number of coffees.

        Args:
            roaster: Roaster name, e.g. "Onyx Coffee Lab".
            name: Coffee name as printed on the bag.
            roast_date: Roast date in YYYY-MM-DD format. Required; ask the user if it isn't known.
            container: Which storage container the beans are in. Ask the user if not stated.
            tasting_notes: Tasting notes from the bag, short phrases, e.g. ["milk chocolate", "citrus"].
            origin: Country/region/farm, if known.
            process: Processing method (washed, natural, honey...), if known.
            roast_level: Roast level (light, medium...), if known.
            notes: Any other short free-form note worth keeping.
        """

        def change(menu: Menu) -> str:
            if len(menu.coffees) >= MAX_COFFEES:
                ids = ", ".join(c.id for c in menu.coffees)
                raise ValueError(
                    f"The menu is full ({MAX_COFFEES} coffees: {ids}). Ask the user which one to remove first."
                )
            coffee = Coffee(
                id=menu.new_id(roaster, name),
                roaster=roaster,
                name=name,
                roast_date=_parse_date(roast_date),
                container=container,
                tasting_notes=tasting_notes,
                origin=origin,
                process=process,
                roast_level=roast_level,
                notes=notes,
            )
            menu.coffees.append(coffee)
            return f"Added coffee id={coffee.id!r}."

        return await mutate(change)

    @beta_async_tool
    async def update_coffee(
        coffee_id: str,
        roaster: str | None = None,
        name: str | None = None,
        roast_date: str | None = None,
        container: Container | None = None,
        tasting_notes: list[str] | None = None,
        origin: str | None = None,
        process: str | None = None,
        roast_level: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Update details of an existing coffee. Only the fields you pass are changed; pass "" to clear an optional text field.

        Args:
            coffee_id: Id of the coffee (see get_menu).
            roaster: New roaster name.
            name: New coffee name.
            roast_date: New roast date, YYYY-MM-DD.
            container: New storage container.
            tasting_notes: Full replacement list of tasting notes.
            origin: Origin ("" clears it).
            process: Process ("" clears it).
            roast_level: Roast level ("" clears it).
            notes: Free-form note ("" clears it).
        """

        def change(menu: Menu) -> str:
            coffee = menu.get(coffee_id)
            if roaster is not None:
                coffee.roaster = roaster
            if name is not None:
                coffee.name = name
            if roast_date is not None:
                coffee.roast_date = _parse_date(roast_date)
            if container is not None:
                coffee.container = container
            if tasting_notes is not None:
                coffee.tasting_notes = tasting_notes
            for attr, value in (("origin", origin), ("process", process), ("roast_level", roast_level), ("notes", notes)):
                if value is not None:
                    setattr(coffee, attr, value or None)
            return f"Updated {coffee_id!r}."

        return await mutate(change)

    @beta_async_tool
    async def remove_coffee(coffee_id: str) -> str:
        """Remove a coffee (and its brew methods) from the menu.

        Args:
            coffee_id: Id of the coffee to remove.
        """

        def change(menu: Menu) -> str:
            coffee = menu.get(coffee_id)
            menu.coffees.remove(coffee)
            return f"Removed {coffee.roaster} {coffee.name}."

        return await mutate(change)

    @beta_async_tool
    async def upsert_brew_method(
        coffee_id: str,
        method: str,
        status: BrewStatus | None = None,
        dose_g: float | None = None,
        yield_g: float | None = None,
        water_g: float | None = None,
        ratio: str | None = None,
        grind: str | None = None,
        temp_c: float | None = None,
        time: str | None = None,
        params: dict[str, str] | None = None,
        notes: str | None = None,
        clear_fields: list[str] | None = None,
    ) -> str:
        """Add a brew method to a coffee, or update it if a method with the same name exists (case-insensitive).
        Only the fields you pass are changed. Use yield_g for espresso output and water_g for filter brews.

        Args:
            coffee_id: Id of the coffee.
            method: Brew method name, e.g. "Espresso", "V60", "AeroPress", "Cold brew".
            status: "dialing_in" while adjusting, "dialed" once the user is happy, "trying" for a first attempt.
            dose_g: Coffee dose in grams.
            yield_g: Beverage yield in grams (espresso).
            water_g: Total brew water in grams (filter/immersion).
            ratio: Brew ratio text, e.g. "1:16".
            grind: Grinder setting, including units/grinder if helpful, e.g. "22 clicks C40".
            temp_c: Water temperature in Celsius.
            time: Total brew/shot time, e.g. "28s" or "3:00".
            params: Other parameters as short key/value text, e.g. {"bloom": "45g / 45s", "pours": "4 x 50g"}. Merged into existing params; set a key to "" to delete it.
            notes: Short tasting/dial-in note shown on the display (one or two short sentences).
            clear_fields: Names of fields to reset to empty, e.g. ["temp_c", "time"].
        """

        def change(menu: Menu) -> str:
            coffee = menu.get(coffee_id)
            existing = coffee.find_method(method)
            brew = existing or BrewMethod(method=method)
            updates = {
                "status": status, "dose_g": dose_g, "yield_g": yield_g, "water_g": water_g, "ratio": ratio,
                "grind": grind, "temp_c": temp_c, "time": time, "notes": notes,
            }
            for key, value in updates.items():
                if value is not None:
                    setattr(brew, key, value)
            for key in clear_fields or []:
                if key not in updates or key == "status":
                    raise ValueError(f"cannot clear field {key!r}")
                setattr(brew, key, None)
            for key, value in (params or {}).items():
                if value:
                    brew.params[key] = value
                else:
                    brew.params.pop(key, None)
            brew.updated_at = now_utc()
            if existing is None:
                coffee.brew_methods.append(brew)
                return f"Added {method} to {coffee_id!r}."
            return f"Updated {brew.method} on {coffee_id!r}."

        return await mutate(change)

    @beta_async_tool
    async def remove_brew_method(coffee_id: str, method: str) -> str:
        """Remove a brew method from a coffee.

        Args:
            coffee_id: Id of the coffee.
            method: Name of the brew method to remove (case-insensitive).
        """

        def change(menu: Menu) -> str:
            coffee = menu.get(coffee_id)
            brew = coffee.find_method(method)
            if brew is None:
                raise ValueError(f"{coffee_id!r} has no brew method {method!r}")
            coffee.brew_methods.remove(brew)
            return f"Removed {brew.method} from {coffee_id!r}."

        return await mutate(change)

    @beta_async_tool
    async def set_menu_title(title: str) -> str:
        """Change the title shown at the top of the display.

        Args:
            title: New title text.
        """

        def change(menu: Menu) -> str:
            menu.title = title
            return f"Title set to {title!r}."

        return await mutate(change)

    @beta_async_tool
    async def render_preview(caption: str | None = None) -> str:
        """Render the display image from the current menu and template, and send it to the user in Telegram.
        Call this after changing the menu or the template so the user can see the result.

        Args:
            caption: Optional short caption for the preview photo.
        """
        try:
            result = await svc.render()
        except Exception as exc:
            return f"Render failed: {exc}"
        await chat.send_photo(result.preview_png, caption)
        return "Preview rendered and sent to the user."

    @beta_async_tool
    async def list_files(directory: str = "templates") -> str:
        """List files in a directory the bot may read (templates/ or the data directory).

        Args:
            directory: Directory path, relative to the project root.
        """
        path = svc.sandbox.readable(directory)
        if not path.is_dir():
            raise ValueError(f"{directory!r} is not a directory")
        entries = sorted(path.rglob("*"))
        lines = [f"{svc.sandbox.display(p)}{'/' if p.is_dir() else ''}" for p in entries if "history" not in p.parts[-2:]]
        return "\n".join(lines[:200]) or "(empty)"

    @beta_async_tool
    async def read_file(path: str) -> str:
        """Read a text file from templates/ or the data directory.

        Args:
            path: File path, relative to the project root, e.g. "templates/menu.css".
        """
        target = svc.sandbox.readable(path)
        if not target.is_file():
            raise ValueError(f"{path!r} does not exist")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{path!r} is too large to read")
        return target.read_text()

    @beta_async_tool
    async def write_file(path: str, content: str) -> str:
        """Create or overwrite a file under templates/ (the display template and CSS).
        The previous version is backed up. Call render_preview afterwards to check the result.

        Args:
            path: File path relative to the project root, e.g. "templates/menu.css".
            content: The complete new file content.
        """
        target = svc.sandbox.writable(path)
        if target.exists():
            backup_dir = svc.store.history_dir / "templates"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = now_utc().strftime("%Y%m%dT%H%M%S")
            (backup_dir / f"{target.name}.{stamp}").write_bytes(target.read_bytes())
        atomic_write_bytes(target, content.encode())
        return f"Wrote {svc.sandbox.display(target)} ({len(content)} chars)."

    @beta_async_tool
    async def run_shell_command(command: str, reason: str) -> str:
        """Run a shell command on the server, in the project directory. The user must approve every
        command in Telegram before it runs; if they decline, don't retry the same command.
        Output is truncated and commands time out after 60 seconds.

        Args:
            command: The shell command to run.
            reason: One short sentence telling the user why you want to run it.
        """
        approved = await chat.request_approval(command, reason)
        if not approved:
            return "The user declined (or didn't answer in time). The command was not run."
        return await run_command(command, cwd=svc.sandbox.project_root)

    return [
        get_menu, add_coffee, update_coffee, remove_coffee, upsert_brew_method, remove_brew_method,
        set_menu_title, render_preview, list_files, read_file, write_file, run_shell_command,
    ]

