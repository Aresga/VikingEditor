#!/usr/bin/env python3
"""
VikingEditor CLI

A command-line version of VikingEditor for decompiling, editing, creating,
and recompiling Valheim character save files (.fch) without a GUI.

The CLI reuses the same save-format code as the desktop application:

  subscripts/fchUtil.py        outer .fch container + SHA-512 signing
  subscripts/playerDataUtil.py nested Player.Save parsing/serialization
  subscripts/newCharacter.py   fresh character generation
  subscripts/itemDatabase.py   Valheim item database management

  python cli.py new MyViking
  python cli.py info characters/Erik.fch
  python cli.py update-items /path/to/Valheim

  python cli.py --help        complete command list
  python cli.py --plain       machine-friendly, ANSI-free output
"""

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from subscripts.fchUtil import decompile_fch, compile_fch
from subscripts.playerDataUtil import (
    unpack_player_data_hex,
    pack_player_data_hex,
)
from subscripts.itemValidation import GRID_WIDTH, validate_item
from data.skills import VALHEIM_SKILLS

# Default grid height; can be extended by the "invrows N" unique.
DEFAULT_GRID_HEIGHT = 4

STAT_KEYS = (
    "max_health",
    "health",
    "max_stamina",
    "stamina",
    "max_eitr",
    "eitr",
)

# ---------------------------------------------------------------
# ANSI styling
# ---------------------------------------------------------------

# Set from --plain, NO_COLOR, or piping output to something
# that cannot render ANSI escapes.
STYLING_ENABLED = True
UNICODE_ENABLED = True

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

CODES = {
    "accent": "96",       # bright cyan
    "title": "1;96",      # bold bright cyan
    "label": "90",        # bright black
    "good": "92",         # bright green
    "warn": "93",         # bright yellow
    "bad": "91",          # bright red
    "value": "97",        # bright white
    "muted": "2;37",      # dim
}


def paint(text, code="value"):
    if not STYLING_ENABLED or not text:
        return text

    return f"\x1b[{CODES.get(code, code)}m{text}\x1b[0m"


def visible_len(text):
    return len(ANSI_ESCAPE.sub("", text))


def pad(text, width):
    return text + " " * max(0, width - visible_len(text))


# ---------------------------------------------------------------
# panels, bars, tables
# ---------------------------------------------------------------

SYMBOLS = {
    "tr_corner": "╭",
    "bl_corner": "╰",
    "br_corner": "╯",
    "horizontal": "─",
    "vertical": "│",
    "full": "█",
    "empty": "░",
    "check": "✔",
    "chevron": "↳",
}

ASCII_SYMBOLS = {
    "tr_corner": ".",
    "bl_corner": "'",
    "br_corner": "'",
    "horizontal": "-",
    "vertical": "|",
    "full": "#",
    "empty": ".",
    "check": "+",
    "chevron": ">>",
}


def symbol(name):
    if UNICODE_ENABLED and name in SYMBOLS:
        return SYMBOLS[name]

    return ASCII_SYMBOLS[name]


def panel(title, lines):
    """
    Draw a rounded box with the title inside the top border, like:

        ╭─ Stats ──────────────────────
        │ Health  [█████████░░░] 25/25
        ╰──────────────────────────────
    """
    inner = 0

    for line in lines:
        inner = max(inner, visible_len(line))

    if title:
        inner = max(inner, visible_len(title) + 2)

    border = symbol("horizontal") * inner

    result = []

    if title:
        gap = inner - visible_len(title) - 1

        result.append(
            paint(
                f"{symbol('tr_corner')}{symbol('horizontal')} ",
                "accent"
            )
            + paint(title, "title")
            + paint(
                f" "
                + symbol("horizontal") * gap,
                "accent"
            )
        )

    else:
        result.append(
            paint(
                f"{symbol('tr_corner')}{border}",
                "accent"
            )
        )

    for line in lines:
        result.append(
            paint(f"{symbol('vertical')} ", "accent")
            + pad(line, inner)
            + paint(f" {symbol('vertical')}", "accent")
        )

    if not lines:
        result.append(paint(symbol("vertical"), "accent"))

    result.append(paint(f"{symbol('bl_corner')}{border}", "accent"))

    return "\n".join(result)


def render_bar(value, maximum, width=28, color=None):
    """
    btop-style use-meter, e.g. [███████░░░░░░░].
    """
    maximum = maximum if maximum and maximum > 0 else 1

    fraction = min(1.0, max(0.0, value / maximum))

    if color is None:
        if fraction >= 0.75:
            color = "good"
        elif fraction >= 0.40:
            color = "warn"
        else:
            color = "bad"

    filled = round(fraction * width)

    bar = (
        symbol("full") * filled
        + symbol("empty") * (width - filled)
    )

    if fraction <= 0:
        return paint(bar, "muted")

    return paint(bar, color)


def flag_tags(item):
    tags = []

    if item.get("equipped"):
        tags.append(paint("[eq]", "accent"))

    if item.get("cheated"):
        tags.append(paint("[cheat]", "bad"))

    return f" {' '.join(tags)}" if tags else ""


# ---------------------------------------------------------------
# helpers
# ---------------------------------------------------------------

@contextlib.contextmanager
def captured_stdout():
    """
    Capture the library's own print output so the CLI can decide
    what is actually shown. Collected warnings are re-printed.
    """
    buffer = io.StringIO()

    with contextlib.redirect_stdout(buffer):
        yield buffer


def flush_warnings(buffer):
    for line in buffer.getvalue().splitlines():
        if "Warning" in line:
            print(paint(line, "warn"))


def resolve_skill(query):
    """
    Resolve a skill by exact id, exact name, or unique substring
    match against the known Valheim skill list.
    """
    text = str(query).strip().lower()

    for skill_id, name in VALHEIM_SKILLS.items():
        if skill_id != 0 and (
            text == name.lower()
            or text == str(skill_id)
        ):
            return skill_id, name

    matches = {
        skill_id: name
        for skill_id, name in VALHEIM_SKILLS.items()
        if skill_id != 0 and text in name.lower()
    }

    if len(matches) == 1:
        skill_id, name = next(iter(matches.items()))
        return skill_id, name

    if len(matches) > 1:
        raise SystemExit(
            f"Ambiguous skill '{query}', matches: "
            + ", ".join(matches.values())
        )

    raise SystemExit(
        f"Unknown skill '{query}'. Use 'list-skills' "
        "to see valid skill names."
    )


def parse_bool(text):
    value = str(text).strip().lower()

    if value in ("true", "1", "yes", "on"):
        return True

    if value in ("false", "0", "no", "off"):
        return False

    raise SystemExit(f"Invalid boolean value: {text}")


def find_skill_entry(player_data, skill_id):
    for skill in player_data.get("skills", []):
        if skill.get("id") == skill_id:
            return skill

    return None


def get_inventory_rows(player_data):
    rows = DEFAULT_GRID_HEIGHT

    if "invrows 6" in player_data.get("uniques", []):
        rows = 6
    elif "invrows 5" in player_data.get("uniques", []):
        rows = 5

    return rows


def find_free_slot(player_data):
    rows = get_inventory_rows(player_data)

    occupied = {
        (item.get("grid_x", 0), item.get("grid_y", 0))
        for item in player_data.get("inventory", [])
    }

    for y in range(rows):
        for x in range(GRID_WIDTH):
            if (x, y) not in occupied:
                return x, y

    raise SystemExit("Inventory is full.")


def find_item(player_data, selector):
    """
    Locate an inventory item by index (display order) or grid
    position, e.g. "3" or "2,1".
    """
    inventory = player_data.get("inventory", [])

    if not inventory:
        raise SystemExit("Inventory is empty.")

    text = str(selector).strip()

    if "," in text:
        try:
            x, y = (
                int(part)
                for part in text.split(",", 1)
            )
        except ValueError:
            raise SystemExit(
                f"Invalid position '{selector}', use X,Y or an index."
            )

        for item in inventory:
            if (
                item.get("grid_x", 0) == x
                and item.get("grid_y", 0) == y
            ):
                return item

        raise SystemExit(f"No item at position ({x}, {y}).")

    try:
        index = int(text)
    except ValueError:
        raise SystemExit(
            f"Invalid selector '{selector}', use X,Y or an index."
        )

    if not 0 <= index < len(inventory):
        raise SystemExit(
            f"Index {index} is out of range "
            f"(0-{len(inventory) - 1})."
        )

    return inventory[index]


def skill_name(skill_id):
    return VALHEIM_SKILLS.get(skill_id, f"#{skill_id}")


def skill_row(skill):
    """
    One btop-style skill use-meter line.
    """
    name = skill_name(skill["id"])

    level = skill.get("level", 0.0)

    bar = render_bar(level, 100.0, width=28)

    xp = skill.get("xp", 0.0)

    return (
        f" {pad(paint(name, 'value'), 18)} "
        f"{bar} "
        f"{paint(f'{level:>5.1f}', 'good')} "
        f"{paint(f'xp {xp:>8.1f}', 'label')}"
    )


def item_word(count):
    return "item" if count == 1 else "items"


def item_row(index, item):
    position = paint(
        f"{item.get('grid_x', 0)},{item.get('grid_y', 0)}",
        "label"
    )

    name = item.get("prefab") or "?"

    quality = paint(f"q{item.get('quality', 1)}", "accent")

    stack = paint(f"{item.get('stack', 1):>4}", "value")

    variant = item.get("variant", 0)

    durability = item.get("durability", 0.0)

    extras = (
        paint(f"v{variant}", "label") + " "
        if variant
        else ""
    )

    return (
        f" {paint(f'{index:>2}', 'label')} "
        f"{symbol('chevron')} "
        f"{position} "
        f"{pad(name, 24)} "
        + f"{extras}"
        + f"{quality} {stack} "
        + paint(f"dur {durability:.0f}", "label")
        + flag_tags(item)
    )


# ---------------------------------------------------------------
# save loading / writing
# ---------------------------------------------------------------

def load_save(fch_path):
    """
    Decompile a .fch file into the root save dictionary and its
    unpacked Player.Save data.
    """
    path = Path(fch_path)

    if not path.is_file():
        raise SystemExit(f"File not found: {path}")

    with captured_stdout() as output:
        save_data = decompile_fch(str(path))

    flush_warnings(output)

    if save_data.get("player_data_hex") is None:
        print(
            paint("Warning: save contains no player data.", "warn"),
            file=sys.stderr
        )

        return save_data, {}

    return (
        save_data,
        unpack_player_data_hex(save_data["player_data_hex"]),
    )


def compile_save(save_data, fch_path):
    """
    Compile a root save dictionary (with player_data_hex already set)
    to a signed .fch file through a temporary wrapper JSON, then
    atomically move it into place.
    """
    fch_path = Path(fch_path)
    fch_path.parent.mkdir(parents=True, exist_ok=True)

    temp_wrapper = fch_path.parent / (
        fch_path.name + ".tmp_wrapper.json"
    )

    temp_fch = fch_path.parent / (
        fch_path.name + ".tmp_compile"
    )

    try:
        with temp_wrapper.open(
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(
                save_data,
                file,
                indent=4,
                ensure_ascii=False
            )

        with captured_stdout() as output:
            compile_fch(str(temp_wrapper), str(temp_fch))

        flush_warnings(output)

        temp_fch.replace(fch_path)

    finally:
        temp_wrapper.unlink(missing_ok=True)
        temp_fch.unlink(missing_ok=True)


def write_player_data(save_data, player_data):
    save_data["player_data_hex"] = pack_player_data_hex(
        player_data
    )


def get_backup_root():
    """
    Match the desktop application: configured backup directory,
    otherwise 'backups' next to the project.
    """
    from ui.valheim_detection import load_config

    config = load_config()

    backup_dir = str(config.get("backup_dir", "")).strip()

    if backup_dir:
        return Path(backup_dir)

    return Path(__file__).resolve().parent / "backups"


def create_backup(source_path, character_name):
    """
    Create a timestamped backup of the original .fch before an
    in-place edit, mirroring the GUI's automatic backup.
    """
    backup_root = get_backup_root() / (character_name or "Viking")
    backup_root.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    backup_path = backup_root / (
        f"{character_name or 'Viking'}_{timestamp}.fch"
    )

    shutil.copy2(source_path, backup_path)

    return backup_path


def cleanup_old_backups(character_name):
    from ui.valheim_detection import load_config

    config = load_config()

    max_backups = config.get(
        "max_backups_per_character",
        15
    )

    if not max_backups:
        return

    backup_dir = get_backup_root() / (character_name or "Viking")

    if not backup_dir.is_dir():
        return

    backups = sorted(
        backup_dir.glob("*.fch"),
        key=lambda path: path.stat().st_mtime,
        reverse=True
    )

    for old_backup in backups[max_backups:]:
        old_backup.unlink()


def print_saved(output_path, backup_path=None):
    print(
        paint(f"{symbol('check')} ", "good")
        + paint("Saved: ", "label")
        + paint(str(output_path), "value")
    )

    if backup_path:
        print(
            "  "
            + paint(symbol("chevron"), "label")
            + " "
            + paint("Backup: ", "label")
            + paint(str(backup_path), "muted")
        )


def save_edits(args, save_data, player_data):
    """
    Finish an editing command: optionally back up the original,
    pack the player data, and write the .fch file.
    """
    output_path = (
        Path(args.out)
        if args.out
        else Path(args.file)
    )

    in_place = (
        output_path.resolve() == Path(args.file).resolve()
    )

    backup_path = None

    if in_place and not args.no_backup:
        should_backup = True

        if args.backup is not None:
            should_backup = args.backup
        else:
            from ui.valheim_detection import load_config

            should_backup = load_config().get(
                "auto_backup",
                True
            )

        if should_backup:
            backup_path = create_backup(
                args.file,
                save_data.get("character_name", "Viking"),
            )

            cleanup_old_backups(
                save_data.get("character_name", "Viking")
            )

    write_player_data(save_data, player_data)

    compile_save(save_data, output_path)

    print_saved(output_path, backup_path)


def add_edit_flag(parser):
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help="Write the result to this path instead of editing in place.",
    )

    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the automatic backup for this run.",
    )

    parser.add_argument(
        "--backup",
        type=parse_bool,
        default=None,
        metavar="BOOL",
        help="Force backups on/off for this run, overriding the config.",
    )


# ---------------------------------------------------------------
# commands
# ---------------------------------------------------------------

def cmd_info(args):
    save_data, player_data = load_save(args.file)

    character_name = save_data.get("character_name", "")

    used_cheats = save_data.get("used_cheats", False)

    # Character

    character_panel = panel(
        character_name or "Character",
        [
            f"{paint('Player ID', 'label')}     {paint(str(save_data.get('player_id')), 'value')}",
            f"{paint('Start seed', 'label')}    {paint(str(save_data.get('start_seed')), 'value')}",
            f"{paint('Created', 'label')}       {paint(str(datetime.fromtimestamp(save_data.get('date_created_unix', 0))), 'value')}",
            f"{paint('Save version', 'label')}  {paint(str(save_data.get('version')), 'value')}"
            f"   {paint('Worlds', 'label')} {paint(str(len(save_data.get('worlds', []))), 'value')}",
            ""
            + pad(paint("Cheats", "label"), 14)
            + (
                paint("used", "bad")
                if used_cheats
                else paint("clean", "good")
            ),
        ],
    )

    # Stats

    def stat_bar(label, value, maximum):
        return (
            f" {pad(paint(label, 'label'), 12)} "
            f"{render_bar(value, maximum)} "
            + paint(
                f"{value:>6.1f} / {maximum:>6.1f}",
                "value"
            )
        )

    guardian = player_data.get("guardian_power", "") or "-"

    stats_panel = panel(
        "Stats",
        [
            stat_bar(
                "Health",
                player_data.get("health", 0.0),
                player_data.get("max_health", 0.0),
            ),
            stat_bar(
                "Stamina",
                player_data.get("stamina", 0.0),
                player_data.get("max_stamina", 0.0),
            ),
            stat_bar(
                "Eitr",
                player_data.get("eitr", 0.0),
                player_data.get("max_eitr", 0.0),
            ),
            "",
            f" {paint('Guardian power', 'label')} "
            + paint(str(guardian), "warn" if guardian != "-" else "muted"),
        ],
    )

    # Skills

    skills = sorted(
        player_data.get("skills", []),
        key=lambda entry: skill_name(entry["id"]),
    )

    skills_panel = panel(
        f"Skills ({len(skills)} unlocked)",
        [skill_row(skill) for skill in skills]
        or [f" {paint('No skills unlocked.', 'muted')}"],
    )

    # Inventory

    inventory = player_data.get("inventory", [])

    inventory_panel = panel(
        f"Inventory ({len(inventory)} {item_word(len(inventory))}, "
        f"grid {GRID_WIDTH}x{get_inventory_rows(player_data)})",
        [
            item_row(index, item)
            for index, item in enumerate(inventory)
        ]
        or [f" {paint('Inventory is empty.', 'muted')}"],
    )

    print()
    print(character_panel)
    print()
    print(stats_panel)
    print()
    print(skills_panel)
    print()
    print(inventory_panel)
    print()


def cmd_export(args):
    save_data, player_data = load_save(args.file)

    output = dict(save_data)

    output.pop("player_data_hex", None)

    output["player_data"] = player_data

    output_path = (
        Path(args.out)
        if args.out
        else Path(args.file).with_suffix(".json")
    )

    with output_path.open(
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            output,
            file,
            indent=4,
            ensure_ascii=False
        )

    print(
        paint(f"{symbol('check')} ", "good")
        + paint("Exported editable JSON: ", "label")
        + paint(str(output_path), "value")
    )


def cmd_build(args):
    with open(args.json, "r", encoding="utf-8") as file:
        save_data = json.load(file)

    player_data = save_data.pop("player_data", None)

    if player_data is not None:
        write_player_data(save_data, player_data)

    if save_data.get("player_data_hex") is None:
        raise SystemExit(
            "JSON has no 'player_data' or 'player_data_hex' to compile."
        )

    compile_save(save_data, args.out)

    print(
        paint(f"{symbol('check')} ", "good")
        + paint("Compiled and signed: ", "label")
        + paint(str(args.out), "value")
    )


def cmd_new(args):
    from subscripts.newCharacter import create_new_character

    save_data = create_new_character(args.name)

    output_path = (
        Path(args.out)
        if args.out
        else Path(f"{args.name.lower()}.fch")
    )

    compile_save(save_data, output_path)

    print(
        paint("Created ", "label")
        + paint("new character", "title")
        + paint(": ", "label")
        + paint(str(output_path), "value")
    )


def cmd_characters(args):
    from ui.valheim_detection import (
        get_valheim_character_save_directory,
    )

    directory = (
        Path(args.dir)
        if args.dir
        else get_valheim_character_save_directory()
    )

    if not directory.is_dir():
        raise SystemExit(f"Character directory not found: {directory}")

    saves = sorted(directory.glob("*.fch"))

    lines = []

    for path in saves:
        try:
            save_data, _ = load_save(path)

            name = save_data.get("character_name", "?")

            cheats = (
                paint("yes", "bad")
                if save_data.get("used_cheats")
                else paint("no", "good")
            )

            lines.append(
                f" {pad(path.name, 32)} "
                f"{paint('name:', 'label')} "
                f"{pad(name, 16)} "
                f"{paint('cheats:', 'label')} {cheats}"
            )
        except Exception as exc:
            lines.append(
                f" {pad(path.name, 32)} "
                f"{paint('unreadable', 'bad')} "
                f"({paint(str(exc), 'label')})"
            )

    print()
    print(
        panel(
            f"Characters in {directory}",
            lines
            or [f" {paint('No .fch files found.', 'muted')}"],
        )
    )
    print()


def cmd_rename(args):
    save_data, player_data = load_save(args.file)

    old_name = save_data.get("character_name", "")

    save_data["character_name"] = args.name.strip()

    if not save_data["character_name"]:
        raise SystemExit("Character name cannot be empty.")

    save_edits(args, save_data, player_data)

    print(
        paint(f"Renamed '{old_name}' -> ", "label")
        + paint(save_data["character_name"], "good")
    )


def cmd_set_stat(args):
    if args.key not in STAT_KEYS:
        raise SystemExit(
            f"Unknown stat '{args.key}'. "
            f"Valid keys: {', '.join(STAT_KEYS)}"
        )

    save_data, player_data = load_save(args.file)

    old = player_data.get(args.key, 0.0)

    player_data[args.key] = float(args.value)

    save_edits(args, save_data, player_data)

    print(
        f"{args.key}: {paint(str(old), 'muted')} -> "
        + paint(str(args.value), "good")
    )


def cmd_set_cheats(args):
    save_data, player_data = load_save(args.file)

    save_data["used_cheats"] = parse_bool(args.value)

    save_edits(args, save_data, player_data)

    state = (
        paint("true", "bad")
        if save_data["used_cheats"]
        else paint("false", "good")
    )

    print(f"Cheats flag set to: {state}")


def cmd_clear_cheated(args):
    save_data, player_data = load_save(args.file)

    cleared = 0

    for item in player_data.get("inventory", []):
        if item.get("cheated"):
            item["cheated"] = False
            cleared += 1

    save_edits(args, save_data, player_data)

    print(
        paint("Cleared cheated flag from ", "label")
        + paint(f"{cleared} item(s).", "good")
    )


def cmd_list_skills(args):
    _, player_data = load_save(args.file)

    unlocked = {
        skill["id"]: skill
        for skill in player_data.get("skills", [])
    }

    rows = [skill_row(skill) for skill in unlocked.values()]

    print()
    print(
        panel(
            f"Skills ({len(unlocked)} unlocked)",
            rows or [f" {paint('No skills unlocked.', 'muted')}"],
        )
    )
    print()

    if args.table:
        locked = [
            skill_id
            for skill_id in sorted(VALHEIM_SKILLS)
            if skill_id != 0 and skill_id not in unlocked
        ]

        missing = [
            f" {paint(skill_name(skill_id), 'muted')} "
            f"{paint('(not unlocked)', 'label')}"
            for skill_id in locked
        ]

        print(
            panel(
                f"Locked skills ({len(locked)})",
                missing or [f" {paint('None.', 'muted')}"],
            )
        )
        print()


def cmd_set_skill(args):
    save_data, player_data = load_save(args.file)

    skill_id, name = resolve_skill(args.skill)

    entry = find_skill_entry(player_data, skill_id)

    if entry is None:
        entry = {
            "id": skill_id,
            "level": 1.0,
            "xp": 0.0,
        }

        player_data.setdefault("skills", []).append(entry)

        print(paint("Unlocked new skill: ", "label") + paint(name, "warn"))

    old_level = entry.get("level", 0.0)

    entry["level"] = float(args.level)

    if args.xp is not None:
        entry["xp"] = float(args.xp)

    save_edits(args, save_data, player_data)

    print(
        f"{name} level: {paint(f'{old_level:.1f}', 'muted')} -> "
        + paint(f"{entry['level']:.1f}", "good")
    )


def cmd_remove_skill(args):
    save_data, player_data = load_save(args.file)

    skill_id, name = resolve_skill(args.skill)

    skills = player_data.get("skills", [])

    remaining = [
        skill
        for skill in skills
        if skill.get("id") != skill_id
    ]

    if len(remaining) == len(skills):
        raise SystemExit(f"Skill '{name}' is not present on this character.")

    player_data["skills"] = remaining

    save_edits(args, save_data, player_data)

    print(paint("Removed skill: ", "label") + paint(name, "muted"))


def apply_to_all_skills(player_data, level, include_locked):
    unlocked_ids = {
        skill["id"]
        for skill in player_data.get("skills", [])
    }

    changed = 0

    for skill_id, name in sorted(VALHEIM_SKILLS.items()):
        if skill_id == 0:
            continue

        if skill_id not in unlocked_ids and not include_locked:
            continue

        entry = find_skill_entry(player_data, skill_id)

        if entry is None:
            entry = {
                "id": skill_id,
                "level": float(level),
                "xp": 0.0,
            }

            player_data.setdefault("skills", []).append(entry)
            changed += 1

            continue

        entry["level"] = float(level)
        entry["xp"] = 0.0
        changed += 1

    return changed


def cmd_max_skills(args):
    save_data, player_data = load_save(args.file)

    changed = apply_to_all_skills(
        player_data,
        args.level,
        include_locked=args.all,
    )

    save_edits(args, save_data, player_data)

    print(
        paint("Set ", "label")
        + paint(f"{changed} skill(s)", "good")
        + paint(f" to level ", "label")
        + paint(f"{args.level:g}", "good")
    )


def cmd_reset_skills(args):
    save_data, player_data = load_save(args.file)

    changed = apply_to_all_skills(
        player_data,
        args.level,
        include_locked=True,
    )

    save_edits(args, save_data, player_data)

    print(
        paint("Reset ", "label")
        + paint(f"{changed} skill(s)", "muted")
        + paint(f" to level ", "label")
        + paint(f"{args.level:g}", "muted")
    )


def cmd_list_items(args):
    _, player_data = load_save(args.file)

    inventory = player_data.get("inventory", [])

    print()
    print(
        panel(
            f"Inventory ({len(inventory)} {item_word(len(inventory))}, "
            f"grid {GRID_WIDTH}x{get_inventory_rows(player_data)})",
            [
                item_row(index, item)
                for index, item in enumerate(inventory)
            ]
            or [f" {paint('Inventory is empty.', 'muted')}"],
        )
    )
    print()


def cmd_add_item(args):
    save_data, player_data = load_save(args.file)

    if args.slot is not None:
        try:
            grid_x, grid_y = (
                int(part)
                for part in str(args.slot).split(",", 1)
            )
        except ValueError:
            raise SystemExit(f"Invalid slot '{args.slot}', use X,Y.")
    else:
        grid_x, grid_y = find_free_slot(player_data)

    item = {
        "prefab": args.prefab,
        "durability": args.durability,
        "grid_x": grid_x,
        "grid_y": grid_y,
        "quality": args.quality,
        "stack": args.stack,
        "variant": args.variant,
        "equipped": args.equipped,
        "cheated": args.cheated,
        "picked_up": True,
        "custom_data": {},
    }

    errors = validate_item(item, player_data)

    if errors:
        raise SystemExit("Invalid item: " + " ".join(errors))

    player_data.setdefault("inventory", []).append(item)

    save_edits(args, save_data, player_data)

    print(
        paint("Added ", "label")
        + paint(args.prefab, "accent")
        + paint(f" at ({grid_x}, {grid_y}), ", "label")
        + paint(f"stack {args.stack}, quality {args.quality}.", "label")
    )


def cmd_remove_item(args):
    save_data, player_data = load_save(args.file)

    item = find_item(player_data, args.selector)

    player_data["inventory"].remove(item)

    save_edits(args, save_data, player_data)

    print(
        paint("Removed ", "label")
        + paint(item.get("prefab") or "?", "accent")
        + paint(
            f" from ({item.get('grid_x', 0)}, "
            f"{item.get('grid_y', 0)}).",
            "label"
        )
    )


def cmd_clear_inventory(args):
    save_data, player_data = load_save(args.file)

    count = len(player_data.get("inventory", []))

    player_data["inventory"] = []

    save_edits(args, save_data, player_data)

    print(
        paint("Removed ", "label")
        + paint(f"{count} item(s)", "good")
        + paint(" from the inventory.", "label")
    )


def cmd_search_items(args):
    from subscripts.itemDatabase import load_item_database

    item_database = load_item_database()

    if not item_database:
        raise SystemExit(
            "Item database is empty. Build it first:\n"
            "  python cli.py update-items /path/to/Valheim"
        )

    prefabs = sorted(set(item_database.values()))

    if args.query:
        words = args.query.lower().split()

        prefabs = [
            prefab
            for prefab in prefabs
            if all(word in prefab.lower() for word in words)
        ]

    lines = [
        f" {paint(prefab, 'value')}"
        if index % 2
        else f" {paint(prefab, 'value')}"
        for index, prefab in enumerate(prefabs)
    ]

    print()
    print(
        panel(
            f"Item database ({len(prefabs)} prefab(s))",
            lines or [f" {paint('No matches.', 'muted')}"],
        )
    )
    print()


def cmd_update_items(args):
    from subscripts.itemDatabase import update_item_database

    valheim_dir = args.valheim_dir

    if not valheim_dir:
        from ui.valheim_detection import (
            find_valheim_installation,
            load_saved_valheim_path,
        )

        saved = load_saved_valheim_path()

        if saved is not None and saved.is_dir():
            valheim_dir = saved
        else:
            valheim_dir = find_valheim_installation()

    if not valheim_dir:
        raise SystemExit(
            "Could not find a Valheim installation. "
            "Pass the game directory, e.g.:\n"
            "  python cli.py update-items /path/to/Valheim"
        )

    print(
        paint("Valheim directory: ", "label")
        + paint(str(valheim_dir), "value")
    )

    item_database = update_item_database(str(valheim_dir))

    if item_database is None:
        print(paint("Item database update cancelled.", "warn"))

        return

    print(
        paint(f"{symbol('check')} ", "good")
        + paint("Loaded ", "label")
        + paint(f"{len(item_database)}", "good")
        + paint(" item prefabs into the database.", "label")
    )


# ---------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="VikingEditor CLI",
        description=(
            "Decompile, edit, create, and recompile Valheim "
            "character save files (.fch) from the command line."
        ),
    )

    parser.add_argument(
        "--plain",
        action="store_true",
        help="Disable colors and Unicode output for scripting.",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="COMMAND",
    )

    # info

    p = subparsers.add_parser(
        "info",
        help="Show a styled summary of a .fch save file.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.set_defaults(func=cmd_info)

    # export

    p = subparsers.add_parser(
        "export",
        help="Export a fully editable JSON representation.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument(
        "out",
        nargs="?",
        default=None,
        help="Output .json path (default: <file>.json).",
    )
    p.set_defaults(func=cmd_export)

    # build

    p = subparsers.add_parser(
        "build",
        help="Compile an exported JSON back into a signed .fch.",
    )

    p.add_argument("json", help="Input .json file (from 'export').")
    p.add_argument("out", help="Output .fch path.")
    p.set_defaults(func=cmd_build)

    # new

    p = subparsers.add_parser(
        "new",
        help="Create a fresh Valheim character as a new .fch file.",
    )

    p.add_argument("name", help="Character name.")
    p.add_argument(
        "out",
        nargs="?",
        default=None,
        help="Output .fch path (default: <name lowercased>.fch).",
    )
    p.set_defaults(func=cmd_new)

    # characters

    p = subparsers.add_parser(
        "characters",
        help="List character saves in Valheim's character directory.",
    )

    p.add_argument(
        "dir",
        nargs="?",
        default=None,
        help="Directory to list (default: auto-detected Valheim characters).",
    )
    p.set_defaults(func=cmd_characters)

    # rename

    p = subparsers.add_parser(
        "rename",
        help="Change the character's name.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument("name", help="New character name.")
    add_edit_flag(p)
    p.set_defaults(func=cmd_rename)

    # set-stat

    p = subparsers.add_parser(
        "set-stat",
        help="Set a core stat (health, stamina, eitr, ...).",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument(
        "key",
        help=f"Stat key, one of: {', '.join(STAT_KEYS)}",
    )
    p.add_argument("value", type=float, help="New value.")
    add_edit_flag(p)
    p.set_defaults(func=cmd_set_stat)

    # set-cheats

    p = subparsers.add_parser(
        "set-cheats",
        help="Toggle the save's 'used cheats' metadata flag.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument("value", help="true or false.")
    add_edit_flag(p)
    p.set_defaults(func=cmd_set_cheats)

    # clear-cheated

    p = subparsers.add_parser(
        "clear-cheated",
        help="Clear the cheated flag from every inventory item.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    add_edit_flag(p)
    p.set_defaults(func=cmd_clear_cheated)

    # list-skills

    p = subparsers.add_parser(
        "list-skills",
        help="List skills; --table includes locked skills.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument(
        "--table",
        action="store_true",
        help="Show all Valheim skills, including locked ones.",
    )
    p.set_defaults(func=cmd_list_skills)

    # set-skill

    p = subparsers.add_parser(
        "set-skill",
        help="Set a skill's level, unlocking it if needed.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument(
        "skill",
        help="Skill name (e.g. 'Swords') or skill id.",
    )
    p.add_argument("level", type=float, help="New level.")
    p.add_argument(
        "--xp",
        type=float,
        default=None,
        help="Optional XP accumulator value.",
    )
    add_edit_flag(p)
    p.set_defaults(func=cmd_set_skill)

    # remove-skill

    p = subparsers.add_parser(
        "remove-skill",
        help="Remove a skill from the save (locks it).",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument("skill", help="Skill name or id.")
    add_edit_flag(p)
    p.set_defaults(func=cmd_remove_skill)

    # max-skills

    p = subparsers.add_parser(
        "max-skills",
        help="Set all unlocked skills to a high level.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument(
        "--level",
        type=float,
        default=100.0,
        help="Target level (default: 100).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Also unlock skills not present on the character.",
    )
    add_edit_flag(p)
    p.set_defaults(func=cmd_max_skills)

    # reset-skills

    p = subparsers.add_parser(
        "reset-skills",
        help="Reset every skill's level and XP.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument(
        "--level",
        type=float,
        default=1.0,
        help="Target level (default: 1).",
    )
    add_edit_flag(p)
    p.set_defaults(func=cmd_reset_skills)

    # list-items

    p = subparsers.add_parser(
        "list-items",
        help="List inventory items with slot and flags.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.set_defaults(func=cmd_list_items)

    # add-item

    p = subparsers.add_parser(
        "add-item",
        help="Add an item to the inventory by prefab name.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument(
        "prefab",
        help="Item prefab name (see 'search-items').",
    )
    p.add_argument(
        "--stack",
        type=int,
        default=1,
        help="Stack size (default: 1).",
    )
    p.add_argument(
        "--quality",
        type=int,
        default=1,
        help="Quality level (default: 1).",
    )
    p.add_argument(
        "--variant",
        type=int,
        default=0,
        help="Style variant (default: 0).",
    )
    p.add_argument(
        "--durability",
        type=float,
        default=100.0,
        help="Durability percentage (default: 100).",
    )
    p.add_argument(
        "--slot",
        default=None,
        metavar="X,Y",
        help="Grid position; placed in the first free slot by default.",
    )
    p.add_argument(
        "--equipped",
        action="store_true",
        help="Mark the item as equipped.",
    )
    p.add_argument(
        "--cheated",
        action="store_true",
        help="Flag the item as spawned in with cheats.",
    )
    add_edit_flag(p)
    p.set_defaults(func=cmd_add_item)

    # remove-item

    p = subparsers.add_parser(
        "remove-item",
        help="Remove an item by index ('3') or slot ('2,1').",
    )

    p.add_argument("file", help="Path to the .fch file.")
    p.add_argument(
        "selector",
        help="Item index from 'list-items' or grid position X,Y.",
    )
    add_edit_flag(p)
    p.set_defaults(func=cmd_remove_item)

    # clear-inventory

    p = subparsers.add_parser(
        "clear-inventory",
        help="Remove every item from the inventory.",
    )

    p.add_argument("file", help="Path to the .fch file.")
    add_edit_flag(p)
    p.set_defaults(func=cmd_clear_inventory)

    # search-items

    p = subparsers.add_parser(
        "search-items",
        help="Search the item database for prefab names.",
    )

    p.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Words to search for, in any order.",
    )
    p.set_defaults(func=cmd_search_items)

    # update-items

    p = subparsers.add_parser(
        "update-items",
        help="Rebuild the item database from installed game files.",
    )

    p.add_argument(
        "valheim_dir",
        nargs="?",
        default=None,
        help="Valheim installation directory (auto-detected by default).",
    )
    p.set_defaults(func=cmd_update_items)

    # Also accept --plain after the subcommand, e.g.
    # "python cli.py info save.fch --plain".
    for command_parser in subparsers.choices.values():
        command_parser.add_argument(
            "--plain",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )

    return parser


def main(argv=None):
    parser = build_parser()

    args = parser.parse_args(argv)

    global STYLING_ENABLED, UNICODE_ENABLED

    if (
        args.plain
        or (
            os.environ.get("NO_COLOR")
            and not os.environ.get("FORCE_COLOR")
        )
        or (not sys.stdout.isatty() and not os.environ.get("FORCE_COLOR"))
    ):
        STYLING_ENABLED = False
        UNICODE_ENABLED = False

    if not getattr(args, "func", None):
        parser.print_help()

        return 0

    args.func(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
