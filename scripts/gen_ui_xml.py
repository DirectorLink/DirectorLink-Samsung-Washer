"""Regenerate the tile sections of src/driver.xml from one table.

Rewrites <proxies>, <capabilities> (Navigator icons per state) and <connections>.
Everything else in driver.xml is edited by hand.

Never change proxies or connections once a version is released: Control4
creates them only when the driver is added, so installed projects depend on them.

Usage:  python scripts/gen_ui_xml.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XML = ROOT / "src" / "driver.xml"
C4Z_NAME = "DirectorLink-Samsung-Washer"
SIZES = (70, 90, 300, 512, 1024)
# Off / Ready / Running / Paused / Finished / Error (STATE), pending (refreshing or a command
# being confirmed), unknown (not set up or no status yet)
WASHER_STATES = ("off", "ready", "running", "paused", "finished", "error", "pending", "unknown")

# binding, proxy name (homeowners see it: no brand), icon folder, states, default icon
TILES = [
    (5001, "Washer", "washer", WASHER_STATES, "unknown"),
]


def icons(folder, state, indent):
    return "\n".join(f'{indent}<Icon width="{s}" height="{s}">controller://driver/{C4Z_NAME}/icons/{folder}/{state}_{s}.png</Icon>'
                     for s in SIZES)


def proxies():
    out = ["\t<proxies>"]
    for i, (b, name, folder, *_rest) in enumerate(TILES):
        primary = ' primary="True"' if i == 0 else ""
        out.append(f'\t\t<proxy proxybindingid="{b}" name="{name}"{primary} image_source="c4z" '
                   f'large_image="icons/{folder}/device_lg.png" small_image="icons/{folder}/device_sm.png">uibutton</proxy>')
    out.append("\t</proxies>")
    return "\n".join(out)


def capabilities():
    out = ["\t<capabilities>"]
    for b, _name, folder, states, default in TILES:
        out += [f'\t\t<navigator_display_option proxybindingid="{b}">', "\t\t\t<display_icons>",
                icons(folder, default, "\t\t\t\t")]
        for st in states:
            out += [f'\t\t\t\t<state id="{st}">', icons(folder, st, "\t\t\t\t\t"), "\t\t\t\t</state>"]
        out += ["\t\t\t</display_icons>", "\t\t</navigator_display_option>"]
    out.append("\t</capabilities>")
    return "\n".join(out)


def connection(cid, name, ctype, classname):
    return (f"\t\t<connection>\n\t\t\t<id>{cid}</id>\n\t\t\t<facing>6</facing>\n"
            f"\t\t\t<connectionname>{name}</connectionname>\n\t\t\t<type>{ctype}</type>\n"
            "\t\t\t<consumer>False</consumer>\n\t\t\t<audiosource>False</audiosource>\n"
            "\t\t\t<videosource>False</videosource>\n\t\t\t<linelevel>False</linelevel>\n"
            f"\t\t\t<classes>\n\t\t\t\t<class>\n\t\t\t\t\t<classname>{classname}</classname>\n"
            "\t\t\t\t</class>\n\t\t\t</classes>\n\t\t</connection>")


def connections():
    out = ["\t<connections>"]
    for b, name, *_rest in TILES:
        out.append(connection(b, f"UIBUTTON {name}", 2, "UIBUTTON"))
    out.append("\t</connections>")
    return "\n".join(out)


def main():
    s = XML.read_text(encoding="utf-8")
    for tag, block in (("proxies", proxies()), ("capabilities", capabilities()), ("connections", connections())):
        s, n = re.subn(rf"\t<{tag}>.*?</{tag}>", lambda _m, b=block: b, s, count=1, flags=re.S)
        if n != 1:
            raise SystemExit(f"<{tag}> block not found in driver.xml")
    XML.write_text(s, encoding="utf-8", newline="\n")
    print(f"Updated {XML.relative_to(ROOT)}: {len(TILES)} tile(s)")


if __name__ == "__main__":
    main()
