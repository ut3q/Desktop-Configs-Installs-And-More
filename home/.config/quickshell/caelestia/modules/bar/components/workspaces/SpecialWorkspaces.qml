pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import Caelestia.Config
import qs.components
import qs.services
import qs.utils

// The special-workspace picker. Everything about how the strip looks and
// scrolls now lives in WsColumn; this only supplies which workspaces go in it
// and what a click does.
WsColumn {
    id: root

    required property ShellScreen screen
    readonly property HyprlandMonitor monitor: Hypr.monitorFor(screen)
    readonly property string activeSpecial: (GlobalConfig.bar.workspaces.perMonitorWorkspaces ? monitor : Hypr.focusedMonitor)?.lastIpcObject.specialWorkspace?.name ?? ""

    indicatorColour: Colours.palette.m3tertiary
    indicatorOnColour: Colours.palette.m3onTertiary

    model: ScriptModel {
        id: specials

        values: Hypr.workspaces.values.filter(w => w.name.startsWith("special:") && (!GlobalConfig.bar.workspaces.perMonitorWorkspaces || w.monitor === root.monitor))
    }

    currentIndex: specials.values.findIndex(w => w.name === root.activeSpecial)

    delegate: SpecialWsDelegate {}

    onActivated: (index, item) => {
        const ws = (item as SpecialWsDelegate)?.modelData;
        if (ws)
            Hypr.dispatch(Hypr.usingLua ? `hl.dsp.workspace.toggle_special("${ws.name.slice(8)}")` : `togglespecialworkspace ${ws.name.slice(8)}`);
        else
            Hypr.dispatch(Hypr.usingLua ? 'hl.dsp.workspace.toggle_special("special")' : "togglespecialworkspace special");
    }

    component SpecialWsDelegate: WsItem {
        id: ws

        required property HyprlandWorkspace modelData
        property int wsId
        property string icon
        property bool hasWindows

        size: label.Layout.preferredHeight + (hasWindows ? windows.implicitHeight + Tokens.padding.extraSmall : 0)

        Component.onCompleted: {
            wsId = modelData.id;
            icon = Icons.getSpecialWsIcon(modelData.name);
            hasWindows = Config.bar.workspaces.showWindowsOnSpecialWorkspaces && modelData.lastIpcObject.windows > 0;
        }

        // Hacky thing cause modelData gets destroyed before the remove anim finishes
        Connections {
            function onIdChanged(): void {
                if (ws.modelData)
                    ws.wsId = ws.modelData.id;
            }

            function onNameChanged(): void {
                if (ws.modelData)
                    ws.icon = Icons.getSpecialWsIcon(ws.modelData.name);
            }

            function onLastIpcObjectChanged(): void {
                if (ws.modelData)
                    ws.hasWindows = root.Config.bar.workspaces.showWindowsOnSpecialWorkspaces && ws.modelData.lastIpcObject.windows > 0;
            }

            target: ws.modelData
        }

        Connections {
            function onShowWindowsOnSpecialWorkspacesChanged(): void {
                if (ws.modelData)
                    ws.hasWindows = root.Config.bar.workspaces.showWindowsOnSpecialWorkspaces && ws.modelData.lastIpcObject.windows > 0;
            }

            target: root.Config.bar.workspaces
        }

        Loader {
            id: label

            asynchronous: true

            Layout.alignment: Qt.AlignHCenter | Qt.AlignTop
            Layout.preferredHeight: Tokens.sizes.bar.innerWidth - Tokens.padding.small

            sourceComponent: ws.icon.length === 1 ? letterComp : iconComp

            Component {
                id: iconComp

                MaterialIcon {
                    fill: 1
                    text: ws.icon
                    verticalAlignment: Qt.AlignVCenter
                }
            }

            Component {
                id: letterComp

                StyledText {
                    text: ws.icon
                    verticalAlignment: Qt.AlignVCenter
                }
            }
        }

        Loader {
            id: windows

            asynchronous: true

            Layout.alignment: Qt.AlignHCenter
            Layout.fillHeight: true
            Layout.preferredHeight: implicitHeight

            visible: active
            active: ws.hasWindows

            sourceComponent: Column {
                spacing: 0

                add: Transition {
                    Anim {
                        properties: "scale"
                        from: 0
                        to: 1
                        easing: Tokens.anim.standardDecel
                    }
                }

                move: Transition {
                    Anim {
                        properties: "scale"
                        to: 1
                        easing: Tokens.anim.standardDecel
                    }
                    Anim {
                        properties: "x,y"
                    }
                }

                Repeater {
                    model: ScriptModel {
                        values: {
                            const windows = Hypr.toplevels.values.filter(c => c.workspace?.id === ws.wsId);
                            const maxIcons = root.Config.bar.workspaces.maxWindowIcons;
                            return maxIcons > 0 ? windows.slice(0, maxIcons) : windows;
                        }
                    }

                    MaterialIcon {
                        required property var modelData

                        grade: 0
                        text: Icons.getAppCategoryIcon(modelData.lastIpcObject.class, "terminal")
                        color: Colours.palette.m3onSurfaceVariant
                    }
                }
            }

            Behavior on Layout.preferredHeight {
                Anim {}
            }
        }
    }
}
