import QtQuick
import QtQuick.Window
import "."

Window {
    id: debugWindow
    objectName: "debugWindow"
    width: 360 * Theme.uiScale; height: 220 * Theme.uiScale
    visible: bridge.visible && bridge.debug
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput
    Rectangle {
        anchors.fill: parent; radius: 8; color: Theme.glass; border.color: Theme.accent; border.width: 1
        Column {
            anchors.fill: parent; anchors.margins: 14; spacing: 7
            Text { text: "DEBUG BUS  /  " + bridge.state; color: Theme.accent; font.family: Theme.mono; font.pixelSize: 10 }
            Text { text: "HEARD  " + bridge.heard; color: Theme.white; font.family: Theme.mono; font.pixelSize: 9; elide: Text.ElideRight; width: parent.width }
            Text { text: "NORMALIZED  " + bridge.normalized; color: Theme.muted; font.family: Theme.mono; font.pixelSize: 9; elide: Text.ElideRight; width: parent.width }
            Text { text: "ACTIONS  " + bridge.actions.join("  →  "); color: Theme.white; font.family: Theme.mono; font.pixelSize: 9; wrapMode: Text.Wrap; width: parent.width }
            Rectangle { width: parent.width; height: 4; color: Qt.alpha(Theme.accent, 0.18); Rectangle { width: parent.width * bridge.level; height: parent.height; color: Theme.accent } }
            Text { text: "ERROR  " + (bridge.error || "none"); color: bridge.error ? Theme.red : Theme.muted; font.family: Theme.mono; font.pixelSize: 9; elide: Text.ElideRight; width: parent.width }
        }
    }
}
