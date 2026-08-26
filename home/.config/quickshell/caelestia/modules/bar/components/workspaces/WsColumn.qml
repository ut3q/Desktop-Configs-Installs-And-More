pragma ComponentBehavior: Bound

import QtQuick
import Caelestia.Config
import qs.components
import qs.components.effects
import qs.services

// The workspace strip, shared by the main desktops and the special-workspace
// picker.
//
// Both are the same object: a vertical list that scrolls under a gradient mask,
// with a rounded pill tracking the current entry and recolouring whatever it
// covers. That used to exist only on the picker - the main column had no fade
// and no scrolling, and paged in blocks of `shown` instead, so going to
// workspace 6 replaced the whole strip rather than moving through it. Callers
// now supply a model, a delegate and the pill colours; everything else lives
// here once.
Item {
    id: root

    property alias model: view.model
    property alias delegate: view.delegate
    property alias spacing: view.spacing
    readonly property alias listView: view

    property int currentIndex

    // Width of the indicator pill. Defaults to the full strip; the main column
    // insets it so the section block shows either side.
    property real itemWidth: width
    property color indicatorColour: Colours.palette.m3primary
    property color indicatorOnColour: Colours.palette.m3onPrimary

    // Sits under the delegates and scrolls with them (the occupied-workspace
    // pills). Given the component rather than a child so it stays inside the
    // mask without being caught by the click handler.
    property Component background

    // index is -1 and item null when the click landed on empty strip.
    signal activated(int index, Item item)

    // Whether each end of the strip is faded.
    //
    // The fade is a "there is more this way" hint, so it only earns its keep
    // while the row it dims is somewhere you have not reached yet. Once that
    // row is the current one, or the neighbour you would step to next, dimming
    // it only hides where you are or where you are about to land - so the fade
    // lifts, and comes back as soon as you move away again. Either end is also
    // squared off outright once there is nothing left to scroll to that way.
    //
    // Both scroll limits carry a pixel of slack, in opposite directions:
    //
    //  - StrictlyEnforceRange settles with a spring, so contentY overshoots by
    //    a fraction of a pixel. A bare `> 0` read that as content above and
    //    flicked the top fade on with nothing hidden behind it.
    //  - At the other end the slack used to be *added* to the limit, so the
    //    strip only counted as "at the bottom" once dragged past the end.
    //    Stepping down to the last row never squared that end off, and the
    //    fade sat there hinting at content that did not exist.
    readonly property bool fadeTop: view.contentY > Tokens.padding.extraSmall && !isFirst(neighbour(0)) && !isFirst(neighbour(-1))
    readonly property bool fadeBottom: view.contentY < view.contentHeight - view.height - Tokens.padding.extraSmall && !isLast(neighbour(0)) && !isLast(neighbour(1))

    // itemAtIndex is a call, not a bindable property. Reading count is what
    // ties these to delegates coming and going; the y/height/contentY reads in
    // isFirst and isLast cover scrolling and rows changing size.
    function neighbour(dir: int): Item {
        return view.count > 0 ? view.itemAtIndex(root.currentIndex + dir) : null;
    }

    // Bounded at both ends: an item the view has built just past the viewport
    // clears the far edge too, and that one is not visible at all.
    function isFirst(item: Item): bool {
        return !!item && item.y <= view.contentY + view.spacing && item.y + item.height > view.contentY;
    }

    function isLast(item: Item): bool {
        return !!item && item.y + item.height >= view.contentY + view.height - view.spacing && item.y < view.contentY + view.height;
    }

    // Only pay for the mask FBO when the strip can actually scroll. A list that
    // fits leaves both ends squared off, so the mask would be fully opaque and
    // the whole effect a no-op.
    layer.enabled: view.contentHeight > view.height
    layer.effect: Mask {
        maskSource: mask
    }

    Item {
        id: mask

        anchors.fill: parent
        layer.enabled: true
        visible: false

        Rectangle {
            anchors.fill: parent
            radius: Tokens.rounding.full

            gradient: Gradient {
                orientation: Gradient.Vertical

                GradientStop {
                    position: 0
                    color: Qt.rgba(0, 0, 0, 0)
                }

                GradientStop {
                    position: 0.3
                    color: Qt.rgba(0, 0, 0, 1)
                }

                GradientStop {
                    position: 0.7
                    color: Qt.rgba(0, 0, 0, 1)
                }

                GradientStop {
                    position: 1
                    color: Qt.rgba(0, 0, 0, 0)
                }
            }
        }

        // Square off whichever end is not currently faded (see fadeTop and
        // fadeBottom); a strip short enough not to scroll gets no fade at all.
        Rectangle {
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right

            radius: Tokens.rounding.full
            implicitHeight: parent.height / 2
            opacity: root.fadeTop ? 0 : 1

            Behavior on opacity {
                Anim {
                    type: Anim.DefaultEffects
                }
            }
        }

        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right

            radius: Tokens.rounding.full
            implicitHeight: parent.height / 2
            opacity: root.fadeBottom ? 0 : 1

            Behavior on opacity {
                Anim {
                    type: Anim.DefaultEffects
                }
            }
        }
    }

    Loader {
        asynchronous: true
        active: root.background !== null

        anchors.fill: parent

        sourceComponent: root.background
    }

    ListView {
        id: view

        anchors.fill: parent
        spacing: Tokens.spacing.medium
        interactive: false

        currentIndex: root.currentIndex
        // StrictlyEnforceRange writes to currentIndex when the strip is
        // dragged, which would drop the binding.
        onCurrentIndexChanged: currentIndex = Qt.binding(() => root.currentIndex)

        preferredHighlightBegin: 0
        preferredHighlightEnd: height
        highlightRangeMode: ListView.StrictlyEnforceRange

        highlightFollowsCurrentItem: false
        highlight: Item {
            y: view.currentItem?.y ?? 0
            implicitHeight: (view.currentItem as WsItem)?.size ?? 0

            Behavior on y {
                Anim {}
            }
        }

        add: Transition {
            Anim {
                properties: "scale"
                from: 0
                to: 1
                easing: Tokens.anim.standardDecel
            }
        }

        remove: Transition {
            Anim {
                property: "scale"
                to: 0.5
                type: Anim.StandardSmall
            }
            Anim {
                property: "opacity"
                to: 0
                type: Anim.StandardSmall
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

        displaced: Transition {
            Anim {
                properties: "scale"
                to: 1
                easing: Tokens.anim.standardDecel
            }
            Anim {
                properties: "x,y"
            }
        }
    }

    Loader {
        asynchronous: true
        active: Config.bar.workspaces.activeIndicator
        anchors.fill: parent

        // Wrapper because an anchored Loader resizes its item to fill; the
        // indicator has to stay free to place itself against the current row.
        sourceComponent: Item {
            ActiveIndicator {
                view: view
                colour: root.indicatorColour
                onColour: root.indicatorOnColour
                implicitWidth: root.itemWidth

                anchors.horizontalCenter: parent.horizontalCenter
            }
        }
    }

    MouseArea {
        property real startY

        anchors.fill: view

        drag.target: view.contentItem
        drag.axis: Drag.YAxis
        drag.maximumY: 0
        drag.minimumY: Math.min(0, view.height - view.contentHeight - Tokens.padding.extraSmall)

        onPressed: event => startY = event.y

        onClicked: event => {
            if (Math.abs(event.y - startY) > drag.threshold)
                return;

            // itemAt/indexAt want content coordinates, not viewport ones.
            const y = event.y + view.contentY;
            root.activated(view.indexAt(event.x, y), view.itemAt(event.x, y));
        }
    }
}
