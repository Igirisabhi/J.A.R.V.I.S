pragma Singleton
import QtQuick

QtObject {
    property bool lightMode: false
    property color uiAccent: "#4FD8FF"
    property real uiScale: 1.0
    property bool reduceMotion: false
    readonly property color accent: uiAccent
    readonly property color amber: "#FFB347"
    readonly property color red: "#FF5470"
    readonly property color white: lightMode ? "#10202A" : "#E8F4FF"
    readonly property color background: lightMode ? "#EAF5FA" : "#05070B"
    readonly property color muted: lightMode ? "#617987" : "#7191A0"
    readonly property color glass: lightMode ? "#DDF3FA" : "#B0050B12"
    readonly property string mono: "Consolas"
    readonly property string sans: "Segoe UI Variable"
}
