pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Caelestia.Config
import qs.components
import qs.services

// Soft backing behind runs of occupied workspaces.
//
// Runs are contiguous *in the strip*, not in workspace numbering - now that the
// column lists the workspaces that exist rather than a fixed block of `shown`,
// ids can jump (1..6 then 41) and only adjacency on screen is meaningful.
Item {
    id: root

    required property ListView view
    required property var wsIds
    required property var occupied

    property list<var> pills: []

    function rebuild(): void {
        if (!occupied || !wsIds)
            return;

        let count = 0;
        let start = -1;

        for (let i = 0; i < wsIds.length; i++) {
            const occ = occupied[wsIds[i]] ?? false;

            if (occ && start < 0)
                start = i;

            if (start >= 0 && (!occ || i === wsIds.length - 1)) {
                const end = occ ? i : i - 1;
                if (pills[count]) {
                    pills[count].start = start;
                    pills[count].end = end;
                } else {
                    pills.push(pillComp.createObject(root, {
                        start: start,
                        end: end
                    }));
                }
                count++;
                start = -1;
            }
        }

        if (pills.length > count)
            pills.splice(count, pills.length - count).forEach(p => p.destroy());
    }

    onOccupiedChanged: rebuild()
    onWsIdsChanged: rebuild()

    Repeater {
        model: ScriptModel {
            values: root.pills.filter(p => p)
        }

        StyledRect {
            id: rect

            required property var modelData

            // count is only here so the lookups re-run as delegates appear;
            // itemAtIndex is a call, not a bindable property.
            readonly property WsItem start: root.view.count > 0 ? root.view.itemAtIndex(modelData.start) as WsItem : null
            readonly property WsItem end: root.view.count > 0 ? root.view.itemAtIndex(modelData.end) as WsItem : null

            anchors.horizontalCenter: root.horizontalCenter

            y: (start?.y ?? 0) - 1 - root.view.contentY
            implicitWidth: Tokens.sizes.bar.innerWidth - Tokens.padding.small + 2
            implicitHeight: start && end ? end.y + end.size - start.y + 2 : 0

            color: Colours.layer(Colours.palette.m3surfaceContainerHigh, 2)
            radius: Tokens.rounding.full

            scale: 0
            Component.onCompleted: scale = 1

            Behavior on scale {
                Anim {
                    easing: Tokens.anim.standardDecel
                }
            }

            Behavior on y {
                Anim {}
            }

            Behavior on implicitHeight {
                Anim {}
            }
        }
    }

    Component {
        id: pillComp

        Pill {}
    }

    component Pill: QtObject {
        property int start
        property int end
    }
}
