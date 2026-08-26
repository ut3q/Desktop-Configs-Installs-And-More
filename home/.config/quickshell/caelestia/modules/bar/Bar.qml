pragma ComponentBehavior: Bound

import "popouts" as BarPopouts
import "components"
import "components/workspaces"
import QtQuick
import QtQuick.Layouts
import Quickshell
import Caelestia.Config
import qs.components
import qs.services

ColumnLayout {
    id: root

    required property ShellScreen screen
    required property ScreenState screenState
    required property BarPopouts.Wrapper popouts
    required property bool fullscreen
    readonly property int vPadding: Tokens.padding.large

    function closeTray(): void {
        if (!Config.bar.tray.compact)
            return;

        for (let i = 0; i < repeater.count; i++) {
            const tray = (repeater.itemAt(i) as EntryWrapper).item as Tray;
            if (tray)
                tray.expanded = false;
        }
    }

    function checkPopout(y: real): void {
        const ch = childAt(width / 2, y) as EntryWrapper;

        if (ch?.entryId !== "tray")
            closeTray();

        if (!ch) {
            popouts.hasCurrent = false;
            return;
        }

        const id = ch.entryId;
        const top = ch.y;

        if (id === "statusIcons" && Config.bar.popouts.statusIcons) {
            const items = (ch.item as StatusIcons).items;
            const icon = items.childAt(items.width / 2, mapToItem(items, 0, y).y);
            if (icon) {
                popouts.currentName = icon.name;
                popouts.currentCenter = Qt.binding(() => icon.mapToItem(root, 0, icon.implicitHeight / 2).y);
                popouts.hasCurrent = true;
            }
        } else if (id === "tray" && Config.bar.popouts.tray) {
            const tray = ch.item as Tray;
            if (!Config.bar.tray.compact || (tray.expanded && !tray.expandIcon.contains(mapToItem(tray.expandIcon, tray.implicitWidth / 2, y)))) {
                const index = Math.floor(((y - top - tray.padding * 2 + tray.spacing) / tray.layout.implicitHeight) * tray.items.count);
                const trayItem = tray.items.itemAt(index);
                if (trayItem) {
                    popouts.currentName = `traymenu${index}`;
                    popouts.currentCenter = Qt.binding(() => trayItem.mapToItem(root, 0, trayItem.implicitHeight / 2).y);
                    popouts.hasCurrent = true;
                } else {
                    popouts.hasCurrent = false;
                }
            } else {
                popouts.hasCurrent = false;
                tray.expanded = true;
            }
        } else if (id === "activeWindow" && Config.bar.popouts.activeWindow && Config.bar.activeWindow.showOnHover) {
            popouts.currentName = id.toLowerCase();
            popouts.currentCenter = (ch.item as Item).mapToItem(root, 0, (ch.item as Item).implicitHeight / 2).y ?? 0;
            popouts.hasCurrent = true;
        }
    }

    function handleWheel(y: real, angleDelta: point): void {
        const ch = childAt(width / 2, y) as EntryWrapper;
        if (ch?.entryId === "workspaces" && Config.bar.scrollActions.workspaces) {
            // Workspace scroll
            const mon = (GlobalConfig.bar.workspaces.perMonitorWorkspaces ? Hypr.monitorFor(screen) : Hypr.focusedMonitor);
            const specialWs = mon?.lastIpcObject.specialWorkspace.name;
            if (specialWs?.length > 0)
                Hypr.dispatch(Hypr.usingLua ? `hl.dsp.workspace.toggle_special("${specialWs.slice(8)}")` : `togglespecialworkspace ${specialWs.slice(8)}`);
            else if (angleDelta.y < 0 || (GlobalConfig.bar.workspaces.perMonitorWorkspaces ? mon.activeWorkspace?.id : Hypr.activeWsId) > 1)
                Hypr.dispatch(Hypr.usingLua ? `hl.dsp.focus({ workspace = "r${angleDelta.y > 0 ? "-" : "+"}1" })` : `workspace r${angleDelta.y > 0 ? "-" : "+"}1`);
        } else if (y < screen.height / 2 && Config.bar.scrollActions.volume) {
            // Volume scroll on top half
            if (angleDelta.y > 0)
                Audio.incrementVolume();
            else if (angleDelta.y < 0)
                Audio.decrementVolume();
        } else if (Config.bar.scrollActions.brightness) {
            // Brightness scroll on bottom half
            const monitor = Brightness.getMonitorForScreen(screen);
            if (angleDelta.y > 0)
                monitor.setBrightness(monitor.brightness + GlobalConfig.services.brightnessIncrement);
            else if (angleDelta.y < 0)
                monitor.setBrightness(monitor.brightness - GlobalConfig.services.brightnessIncrement);
        }
    }

    // Waybar-style sections. Config.bar.entries already separates the groups
    // with "spacer" entries, so a run of consecutive non-spacer entries IS a
    // section -- workspaces top-left, the window label middle-left, and
    // tray/clock/status/power bottom-left. Each run gets one rounded block
    // instead of the shell painting one continuous strip down the screen.
    //
    // runs[] holds [firstIndex, lastIndex] pairs against the same filtered
    // model the Repeater below uses, so the indices line up with itemAt().
    readonly property var runs: {
        const out = [];
        const es = Config.bar.entries.filter(e => e.enabled ?? true);
        let start = -1;
        for (let i = 0; i < es.length; i++) {
            if (es[i].id === "spacer") {
                if (start >= 0)
                    out.push([start, i - 1]);
                start = -1;
            } else if (start < 0) {
                start = i;
            }
        }
        if (start >= 0)
            out.push([start, es.length - 1]);
        return out;
    }

    // Section block geometry. The blocks ARE the bar's visible width -- there is
    // no wider strip behind them any more -- so this and BarWrapper.contentWidth
    // are the same number by definition. Both read the token rather than
    // repeating a literal, so `sizes.bar.innerWidth` in shell-tokens.json stays
    // the single knob; the components inside (clock, tray, status icons,
    // workspaces) already size themselves off it.
    readonly property int sectionPadV: Tokens.padding.large      // clearance above/below the entries
    readonly property int sectionWidth: Tokens.sizes.bar.innerWidth
    readonly property int sectionX: 0                            // left edge within the bar
    readonly property int sectionRadius: Tokens.rounding.large

    // -1 unless `i` starts a run, in which case the run's last index.
    function runEnd(i: int): int {
        for (const r of root.runs)
            if (r[0] === i)
                return r[1];
        return -1;
    }

    spacing: Tokens.spacing.medium

    Repeater {
        id: repeater

        model: ScriptModel {
            values: root.Config.bar.entries.filter(e => e.enabled ?? true)
        }

        DelegateChooser {
            role: "id"

            DelegateChoice {
                roleValue: "spacer"
                delegate: EntryWrapper {
                    Layout.fillHeight: true
                }
            }
            DelegateChoice {
                roleValue: "logo"
                delegate: EntryWrapper {
                    OsIcon {
                        objectName: "taskbarLogo"
                    }
                }
            }
            DelegateChoice {
                roleValue: "workspaces"
                delegate: EntryWrapper {
                    Workspaces {
                        objectName: "taskbarWorkspaces"
                        screen: root.screen
                        fullscreen: root.fullscreen
                    }
                }
            }
            DelegateChoice {
                roleValue: "activeWindow"
                delegate: EntryWrapper {
                    ActiveWindow {
                        objectName: "taskbarActiveWindow"
                        bar: root
                        monitor: Brightness.getMonitorForScreen(root.screen)
                    }
                }
            }
            DelegateChoice {
                roleValue: "tray"
                delegate: EntryWrapper {
                    Tray {
                        objectName: "taskbarTray"
                    }
                }
            }
            DelegateChoice {
                roleValue: "clock"
                delegate: EntryWrapper {
                    Clock {
                        objectName: "taskbarClock"
                    }
                }
            }
            DelegateChoice {
                roleValue: "statusIcons"
                delegate: EntryWrapper {
                    StatusIcons {
                        objectName: "taskbarStatusIcons"
                    }
                }
            }
            DelegateChoice {
                roleValue: "power"
                delegate: EntryWrapper {
                    Power {
                        objectName: "taskbarPowerButton"
                        screenState: root.screenState
                    }
                }
            }
        }
    }

    component EntryWrapper: Item {
        id: wrapper

        required property var modelData
        required property int index
        default property Item item
        readonly property string entryId: modelData.id

        // >= 0 only on the first entry of a run; that entry draws the block for
        // the whole run. Item does not clip, so one child can cover its
        // siblings below it -- which keeps the layout tree identical to stock.
        // That matters: checkPopout() and handleWheel() both resolve entries
        // with childAt() on root, and EntryWrapper's margins index against
        // repeater.count. Nesting entries inside real section containers would
        // break all of it.
        readonly property int runLast: root.runEnd(index)

        property Item sectionBg: StyledRect {
            visible: wrapper.runLast >= 0

            // wrapper.x is the entry's own Layout.leftMargin, which differs per
            // entry because it centres that entry inside the block. Subtracting
            // it pins every block to the same absolute x regardless of which
            // entry happens to start the run.
            x: root.sectionX - wrapper.x
            y: -root.sectionPadV
            width: root.sectionWidth
            height: {
                const last = repeater.itemAt(wrapper.runLast);
                const bottom = last ? last.y + last.height : wrapper.y + wrapper.height;
                return bottom - wrapper.y + root.sectionPadV * 2;
            }

            color: Colours.barSurface

            // The blocks sit flush against the left screen edge, so rounding
            // those two corners just opens a wallpaper notch against the bezel.
            // Round only the two corners that face into the desktop.
            topLeftRadius: 0
            bottomLeftRadius: 0
            topRightRadius: root.sectionRadius
            bottomRightRadius: root.sectionRadius
        }

        Layout.topMargin: index === 0 ? root.vPadding : 0
        Layout.bottomMargin: index === repeater.count - 1 ? root.vPadding : 0

        // Centre each entry inside the SECTION BLOCK, not inside the full bar.
        // Stock centred on the bar (Qt.AlignHCenter), which was right when the
        // background was a full-width strip; with a narrower, left-aligned
        // block that leaves every icon sitting off the block's right edge.
        Layout.alignment: Qt.AlignLeft
        Layout.leftMargin: root.sectionX + (root.sectionWidth - implicitWidth) / 2

        implicitWidth: item?.implicitWidth ?? 0
        implicitHeight: item?.implicitHeight ?? 0

        // sectionBg first so it renders behind the entry itself.
        children: item ? [sectionBg, item] : [sectionBg]
    }
}
