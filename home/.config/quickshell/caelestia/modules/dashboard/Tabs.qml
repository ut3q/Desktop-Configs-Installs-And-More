pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import QtQuick.Templates
import Quickshell
import Caelestia.Config
import qs.components
import qs.components.controls
import qs.services

Item {
    id: root

    required property real nonAnimWidth
    required property ScreenState screenState
    required property var tabs

    readonly property alias count: bar.count

    implicitHeight: bar.implicitHeight + bar.anchors.topMargin + indicator.implicitHeight + indicator.anchors.topMargin + separator.implicitHeight

    TabBar {
        id: bar

        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.topMargin: Tokens.sizes.dashboard.tabIndicatorSpacing

        currentIndex: root.screenState.dashboardTab
        onCurrentIndexChanged: root.screenState.dashboardTab = currentIndex

        implicitHeight: contentHeight
        background: null
        contentItem: RowLayout {
            spacing: 0

            Repeater {
                model: bar.contentModel
            }
        }

        Repeater {
            model: ScriptModel {
                values: root.tabs
            }

            delegate: Tab {
                required property var modelData

                iconName: modelData.iconName
                text: modelData.text
            }
        }
    }

    Item {
        id: indicator

        anchors.top: bar.bottom
        anchors.topMargin: 5

        implicitWidth: {
            const tab = bar.currentItem;
            if (tab)
                return tab.implicitWidth;
            const width = (root.nonAnimWidth - bar.spacing * (bar.count - 1)) / bar.count;
            return width;
        }
        implicitHeight: 3

        x: {
            const tab = bar.currentItem;
            const width = (root.nonAnimWidth - bar.spacing * (bar.count - 1)) / bar.count;
            const tabWidth = tab?.implicitWidth ?? width;
            return width * bar.currentIndex + (width - tabWidth) / 2;
        }

        clip: true

        StyledRect {
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            implicitHeight: parent.implicitHeight * 2

            color: Colours.palette.m3primary
            radius: Tokens.rounding.full
        }

        Behavior on x {
            Anim {}
        }

        Behavior on implicitWidth {
            Anim {}
        }
    }

    StyledRect {
        id: separator

        anchors.top: indicator.bottom
        anchors.left: parent.left
        anchors.right: parent.right

        implicitHeight: 1
        color: Colours.palette.m3outlineVariant
    }

    component Tab: TabButton {
        id: tab

        required property string iconName
        readonly property bool current: TabBar.tabBar.currentItem === this

        Layout.fillWidth: true
        Layout.preferredWidth: 1 // Uniform width across all tabs
        implicitWidth: implicitContentWidth
        implicitHeight: implicitContentHeight
        background: null

        contentItem: CustomMouseArea {
            id: mouse

            function onWheel(event: WheelEvent): void {
                if (event.angleDelta.y < 0)
                    root.screenState.dashboardTab = Math.min(root.screenState.dashboardTab + 1, bar.count - 1);
                else if (event.angleDelta.y > 0)
                    root.screenState.dashboardTab = Math.max(root.screenState.dashboardTab - 1, 0);
            }

            // Icon beside the label rather than stacked above it. Stacked, the
            // tab strip cost icon.height + label.height (~38px) at the top of
            // every dashboard tab; side by side it costs the taller of the two.
            implicitWidth: content.implicitWidth
            implicitHeight: content.implicitHeight

            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor

            onPressed: root.screenState.dashboardTab = tab.TabBar.index

            StateLayer {
                id: stateLayer

                anchors.fill: undefined
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                implicitHeight: parent.height + Tokens.sizes.dashboard.tabIndicatorSpacing * 2

                radius: Tokens.rounding.medium
                color: tab.current ? Colours.palette.m3primary : Colours.palette.m3onSurface
                onClicked: root.screenState.dashboardTab = tab.TabBar.index
            }

            RowLayout {
                id: content

                anchors.centerIn: parent
                spacing: Tokens.spacing.small

                MaterialIcon {
                    id: icon

                    Layout.alignment: Qt.AlignVCenter

                    text: tab.iconName
                    color: tab.current ? Colours.palette.m3primary : Colours.palette.m3onSurfaceVariant
                    fill: tab.current ? 1 : 0
                    fontStyle: Tokens.font.icon.small

                    Behavior on fill {
                        Anim {
                            type: Anim.DefaultEffects
                        }
                    }
                }

                StyledText {
                    id: label

                    Layout.alignment: Qt.AlignVCenter

                    text: tab.text
                    color: tab.current ? Colours.palette.m3primary : Colours.palette.m3onSurfaceVariant
                }
            }
        }
    }
}
