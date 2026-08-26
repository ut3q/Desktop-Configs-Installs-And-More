pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import Quickshell.Wayland
import Caelestia.Config
import qs.components
import qs.services

Item {
    id: root

    required property ShellScreen screen
    required property HyprlandToplevel client

    Layout.preferredWidth: preview.implicitWidth + Tokens.padding.extraLargeIncreased
    Layout.fillHeight: true

    StyledClippingRect {
        id: preview

        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.bottom: label.top
        anchors.topMargin: Tokens.padding.large
        anchors.bottomMargin: Tokens.spacing.medium

        // Through the Loader's *item*, not the Loader: a Loader picks up its
        // item's implicit size on load but keeps the last value after
        // unloading, so binding to viewLoader.implicitWidth would leave a
        // stale width behind once the client goes away. Going via item
        // reproduces the original ScreencopyView behaviour exactly (0 when
        // there is nothing to show, so the "No active client" placeholder
        // governs the panel width).
        implicitWidth: viewLoader.item ? viewLoader.item.implicitWidth : 0

        color: Colours.tPalette.m3surfaceContainer
        radius: Tokens.rounding.medium

        Loader {
            asynchronous: true
            anchors.centerIn: parent
            active: !root.client

            sourceComponent: ColumnLayout {
                spacing: 0

                MaterialIcon {
                    Layout.alignment: Qt.AlignHCenter
                    text: "web_asset_off"
                    color: Colours.palette.m3outline
                    fontStyle: Tokens.font.icon.builders.extraLarge.scale(3).build()
                }

                StyledText {
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("No active client")
                    color: Colours.palette.m3outline
                    font: Tokens.font.body.builders.large.size(28).weight(Font.Medium).build()
                }

                StyledText {
                    Layout.alignment: Qt.AlignHCenter
                    text: qsTr("Try switching to a window")
                    color: Colours.palette.m3outline
                    font: Tokens.font.body.large
                }
            }
        }

        // Same hazard as the bar's active-window preview: a live
        // ext-image-copy-capture session re-pointed at whatever client this
        // panel is showing. Hyprland builds each frame by reading transform()
        // off the capture source, so a client that closed while the panel was
        // open takes the compositor down with it. Rebuild per client instead of
        // rebinding, and hold no session when there is no client.
        Loader {
            id: viewLoader

            readonly property var toplevel: root.client?.wayland ?? null // qmllint disable unresolved-type

            function rebuild(): void {
                active = false; // destroys the old session before the next one opens
                if (toplevel && visible)
                    active = true;
            }

            anchors.centerIn: parent

            active: false
            onToplevelChanged: rebuild()
            onVisibleChanged: rebuild()
            Component.onCompleted: rebuild()

            sourceComponent: ScreencopyView {
                id: view

                captureSource: viewLoader.toplevel
                live: true

                constraintSize.width: root.client ? preview.height * Math.min(root.screen.width / root.screen.height, root.client.lastIpcObject.size[0] / root.client.lastIpcObject.size[1]) : preview.height
                constraintSize.height: preview.height
            }
        }
    }

    StyledText {
        id: label

        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Tokens.padding.large

        animate: true
        text: {
            const client = root.client;
            if (!client)
                return qsTr("No active client");

            const mon = client.monitor;
            return qsTr("%1 on monitor %2 at %3, %4").arg(client.title).arg(mon.name).arg(client.lastIpcObject.at[0]).arg(client.lastIpcObject.at[1]);
        }
    }
}
