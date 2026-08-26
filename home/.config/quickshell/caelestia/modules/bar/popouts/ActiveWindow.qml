import QtQuick
import QtQuick.Layouts
import Quickshell.Wayland
import Quickshell.Widgets
import Caelestia.Config
import qs.components
import qs.services
import qs.utils

Item {
    id: root

    required property PopoutState popouts

    implicitWidth: Hypr.activeToplevel ? child.implicitWidth : -Tokens.padding.extraLargeIncreased
    implicitHeight: child.implicitHeight

    Column {
        id: child

        anchors.centerIn: parent
        spacing: Tokens.spacing.medium

        RowLayout {
            id: detailsRow

            anchors.left: parent.left
            anchors.right: parent.right
            spacing: Tokens.spacing.medium

            IconImage {
                id: icon

                asynchronous: true
                Layout.alignment: Qt.AlignVCenter
                implicitSize: details.implicitHeight
                source: Icons.getAppIcon(Hypr.activeToplevel?.lastIpcObject.class ?? "", "image-missing")
            }

            ColumnLayout {
                id: details

                spacing: 0
                Layout.fillWidth: true

                StyledText {
                    Layout.fillWidth: true
                    text: Hypr.activeToplevel?.title ?? ""
                    font: Tokens.font.body.medium
                    elide: Text.ElideRight
                }

                StyledText {
                    Layout.fillWidth: true
                    text: Hypr.activeToplevel?.lastIpcObject.class ?? ""
                    color: Colours.palette.m3onSurfaceVariant
                    elide: Text.ElideRight
                }
            }

            Item {
                implicitWidth: expandIcon.implicitHeight + Tokens.padding.small
                implicitHeight: expandIcon.implicitHeight + Tokens.padding.small

                Layout.alignment: Qt.AlignVCenter

                StateLayer {
                    radius: Tokens.rounding.large
                    onClicked: root.popouts.detachRequested("winfo")
                }

                MaterialIcon {
                    id: expandIcon

                    anchors.centerIn: parent
                    anchors.horizontalCenterOffset: font.pointSize * 0.05

                    text: "chevron_right"

                    fontStyle: Tokens.font.icon.large
                }
            }
        }

        ClippingWrapperRectangle {
            color: "transparent"
            radius: Tokens.rounding.medium

            // Was a single long-lived ScreencopyView whose captureSource was
            // bound straight to the focused toplevel, kept live for as long as
            // the popout was on screen. That re-points a running
            // ext-image-copy-capture session at a different -- or
            // just-destroyed -- window every time focus moves, and every
            // Hyprland crash report in ~/.cache/hyprland bottoms out in
            // exactly that path:
            //
            //   Screenshare::CScreenshareFrame::transform() const
            //   CImageCopyCaptureFrame::CImageCopyCaptureFrame(...)
            //   CExtForeignToplevelImageCaptureSourceManagerV1::setDestroy(...)
            //
            // The frame constructor reads transform() off the capture source,
            // so a source that disappeared between the focus change and the
            // next frame request is a null dereference in the compositor.
            //
            // Tearing the view down and building a new one per toplevel means a
            // session is only ever pointed at the window it was created for,
            // and no session exists at all while there is no active toplevel.
            //
            // A Loader keeps its last item's implicit size after unloading, so
            // the wrapper does not shrink back when the toplevel goes away.
            // That is invisible here: root.implicitWidth already collapses the
            // whole popout to a negative width when there is no active
            // toplevel, so the stale box is never laid out.
            Loader {
                id: previewLoader

                readonly property var toplevel: Hypr.activeToplevel?.wayland ?? null // qmllint disable unresolved-type

                function rebuild(): void {
                    active = false; // destroys the old session before the next one opens
                    if (toplevel && visible)
                        active = true;
                }

                active: false
                onToplevelChanged: rebuild()
                onVisibleChanged: rebuild()
                Component.onCompleted: rebuild()

                sourceComponent: ScreencopyView {
                    id: preview

                    captureSource: previewLoader.toplevel
                    live: true

                    constraintSize.width: Tokens.sizes.bar.windowPreviewSize
                    constraintSize.height: Tokens.sizes.bar.windowPreviewSize
                }
            }
        }
    }
}
