pragma Singleton

import QtQuick
import Quickshell
import Caelestia.Config

Singleton {
    property alias enabled: clock.enabled
    readonly property date date: clock.date
    readonly property int hours: clock.hours
    readonly property int minutes: clock.minutes
    readonly property int seconds: clock.seconds

    readonly property string timeStr: format(GlobalConfig.services.useTwelveHourClock ? "hh:mm:A" : "hh:mm")
    readonly property list<string> timeComponents: timeStr.split(":")
    readonly property string hourStr: timeComponents[0] ?? ""
    readonly property string minuteStr: timeComponents[1] ?? ""
    readonly property string amPmStr: timeComponents[2] ?? ""

    function format(fmt: string): string {
        return Qt.formatDateTime(clock.date, fmt);
    }

    SystemClock {
        id: clock

        // Nothing in the shell renders seconds: every consumer is format(),
        // hourStr, minuteStr or amPmStr, all of which are minute-granular.
        // Seconds precision woke the process and re-evaluated the whole
        // timeStr -> split -> TextMetrics chain 60x more often than any
        // displayed value could change.
        precision: SystemClock.Minutes
    }
}
