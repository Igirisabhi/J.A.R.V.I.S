import QtQuick
import QtQuick.Window
import "."

Window {
    id: panel
    objectName: "panelWindow"
    width: 300 * Theme.uiScale; height: 450 * Theme.uiScale
    visible: bridge.visible && bridge.panel_visible
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput
    Rectangle {
        anchors.fill: parent; radius: 10; color: Theme.glass; border.color: bridge.state === "ERROR" || bridge.detail.toLowerCase().indexOf("reject") >= 0 ? Theme.red : Theme.accent; border.width: 1
        Column {
            anchors.fill: parent; anchors.margins: 18; spacing: 13
            Text { text: "WORKSPACE  /  LIVE"; color: Theme.accent; font.family: Theme.mono; font.pixelSize: 11 }
            Rectangle { width: parent.width; height: 1; color: Qt.alpha(Theme.accent, 0.35) }
            Text { text: "ROOT"; color: Theme.muted; font.family: Theme.mono; font.pixelSize: 9 }
            Text { width: parent.width; text: bridge.workspace || "Not set"; wrapMode: Text.WrapAnywhere; color: Theme.white; font.family: Theme.mono; font.pixelSize: 10 }
            Text { text: "RECENT COMMANDS"; color: Theme.accent; font.family: Theme.mono; font.pixelSize: 9; topPadding: 10 }
            Text { width: parent.width; text: bridge.heard || "No commands yet"; wrapMode: Text.Wrap; color: Theme.white; font.family: Theme.sans; font.pixelSize: 11 }
            Text { width: parent.width; text: bridge.detail; wrapMode: Text.Wrap; color: bridge.detail.toLowerCase().indexOf("reject") >= 0 ? Theme.amber : Theme.muted; font.family: Theme.mono; font.pixelSize: 9 }
            Text { text: "RECENT FILES"; color: Theme.accent; font.family: Theme.mono; font.pixelSize: 9; topPadding: 10 }
            Text { width: parent.width; text: bridge.recentFiles.length ? bridge.recentFiles.join("\n") : "None"; wrapMode: Text.Wrap; color: Theme.white; font.family: Theme.mono; font.pixelSize: 9 }
        }
    }
}
