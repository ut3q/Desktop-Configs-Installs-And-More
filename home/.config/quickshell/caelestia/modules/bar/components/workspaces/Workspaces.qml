pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Effects
import Quickshell
import Quickshell.Hyprland
import Caelestia.Config
import qs.components
import qs.services

StyledClippingRect {
    id: root

    required property ShellScreen screen
    required property bool fullscreen

    readonly property HyprlandMonitor monitor: Hypr.monitorFor(screen)
    readonly property bool onSpecial: (GlobalConfig.bar.workspaces.perMonitorWorkspaces ? monitor : Hypr.focusedMonitor)?.lastIpcObject.specialWorkspace?.name !== ""
    readonly property int activeWsId: GlobalConfig.bar.workspaces.perMonitorWorkspaces ? (monitor.activeWorkspace?.id ?? 1) : Hypr.activeWsId

    readonly property var occupied: {
        const occ = {};
        for (const ws of Hypr.workspaces.values)
            occ[ws.id] = ws.lastIpcObject.windows > 0;
        return occ;
    }

    // The strip holds the workspaces that actually exist, padded out to `shown`
    // so a fresh session still offers 1..5.
    //
    // It cannot be a 1..max range: Hyprland hands out ids like 41 and that
    // would mean 41 rows. It is no longer paged in blocks of `shown` either -
    // that made workspace 6 replace the whole column rather than scroll into
    // it, which is exactly what the special picker never did.
    readonly property var wsIds: {
        const ids = new Set();
        for (let i = 1; i <= Config.bar.workspaces.shown; i++)
            ids.add(i);
        for (const ws of Hypr.workspaces.values)
            if (ws.id > 0 && (!GlobalConfig.bar.workspaces.perMonitorWorkspaces || ws.monitor === monitor))
                ids.add(ws.id);
        ids.add(activeWsId);
        return Array.from(ids).sort((a, b) => a - b);
    }

    property real blur: onSpecial ? 1 : 0

    // Row metrics. rowHeight must match the digit height in Workspace.qml; the
    // strip is a fixed `shown` rows tall so the bar block never resizes when a
    // workspace appears - it scrolls instead.
    readonly property int rowHeight: 22
    readonly property int rowSpacing: 6 // was spacing.extraSmall; the column needed room to breathe
    readonly property int inset: 5

    // Explicit for the same reason as sectionWidth above: innerWidth is 35 and
    // is not configurable, which left the workspace column far too cramped.
    // 38 inside a 44 block gives a 3px reveal of the darker section either side.
    implicitWidth: 38
    implicitHeight: Config.bar.workspaces.shown * (rowHeight + rowSpacing) - rowSpacing + inset * 2

    color: Colours.barPill
    // Was rounding.full (a pill). The section blocks around it use
    // rounding.large, so match them rather than sitting as a capsule inside a
    // rounded rectangle.
    radius: Tokens.rounding.large

    Item {
        anchors.fill: parent
        scale: root.onSpecial ? 0.8 : 1
        opacity: root.onSpecial ? 0.5 : 1
        visible: !root.fullscreen

        layer.enabled: root.blur > 0
        layer.effect: MultiEffect {
            blurEnabled: true
            blur: root.blur
            blurMax: 32
        }

        WsColumn {
            id: column

            anchors.fill: parent
            anchors.margins: root.inset

            spacing: root.rowSpacing
            itemWidth: Tokens.sizes.bar.innerWidth - Tokens.padding.small

            model: ScriptModel {
                values: root.wsIds
            }

            currentIndex: root.wsIds.indexOf(root.activeWsId)

            delegate: Workspace {
                activeWsId: root.activeWsId
                occupied: root.occupied
            }

            background: Config.bar.workspaces.occupiedBg ? occupiedBgComp : null

            onActivated: (index, item) => {
                const ws = (item as Workspace)?.ws;
                if (!ws)
                    return;
                if (Hypr.activeWsId !== ws)
                    Hypr.dispatch(Hypr.usingLua ? `hl.dsp.focus({ workspace = "${ws}" })` : `workspace ${ws}`);
                else
                    Hypr.dispatch(Hypr.usingLua ? 'hl.dsp.workspace.toggle_special("special")' : "togglespecialworkspace special");
            }
        }

        Component {
            id: occupiedBgComp

            OccupiedBg {
                view: column.listView
                wsIds: root.wsIds
                occupied: root.occupied
            }
        }

        Behavior on scale {
            Anim {}
        }

        Behavior on opacity {
            Anim {
                type: Anim.DefaultEffects
            }
        }
    }

    Loader {
        id: specialWs

        asynchronous: true

        anchors.fill: parent
        anchors.margins: root.inset

        active: opacity > 0

        scale: root.onSpecial ? 1 : 0.5
        opacity: root.onSpecial ? 1 : 0

        sourceComponent: SpecialWorkspaces {
            screen: root.screen
            spacing: root.rowSpacing
        }

        Behavior on scale {
            Anim {}
        }

        Behavior on opacity {
            Anim {
                type: Anim.DefaultEffects
            }
        }
    }

    Behavior on blur {
        Anim {
            type: Anim.StandardSmall
        }
    }
}
