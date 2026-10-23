import QtQuick
import QtQuick.Window
import "."

Item {
    id: wheel
    property real radius: width * 0.36
    property int currentIndex: {
        var found = 0
        for (var i = 0; i < bridge.steps.length; i++) if (bridge.steps[i].state === "running") found = i
        return found
    }
    Canvas {
        anchors.fill: parent
        onPaint: {
            var ctx = getContext("2d"); ctx.reset()
            if (!bridge.steps.length) return
            var cx = width / 2, cy = height / 2
            ctx.setLineDash([3, 7]); ctx.strokeStyle = Qt.alpha(Theme.accent, 0.22); ctx.lineWidth = 1
            ctx.beginPath(); ctx.arc(cx, cy, wheel.radius, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([])
        }
    }
    Repeater {
        model: bridge.steps
        delegate: Rectangle {
            required property var modelData
            required property int index
            property real angle: (index * Math.PI * 2 / Math.max(bridge.steps.length, 1)) - Math.PI / 2
            width: 27; height: 27; radius: 14
            x: wheel.width / 2 + Math.cos(angle) * wheel.radius - width / 2
            y: wheel.height / 2 + Math.sin(angle) * wheel.radius - height / 2
            scale: modelData.state === "running" ? 1.2 : 1.0
            color: modelData.state === "done" ? Theme.accent : "#18070B10"
            border.color: modelData.state === "failed" ? Theme.red : modelData.state === "running" ? Theme.accent : Theme.muted
            border.width: 1
            Behavior on scale { SpringAnimation { spring: 6; damping: 0.42 } }
            Text { anchors.centerIn: parent; text: modelData.state === "done" ? "✓" : modelData.state === "failed" ? "×" : String(index + 1); color: modelData.state === "done" ? Theme.background : Theme.white; font.family: Theme.mono; font.pixelSize: 10 }
        }
    }
    Rectangle {
        anchors.centerIn: parent; width: 126; height: 126; radius: 64
        color: Theme.glass; border.color: bridge.steps.length && bridge.steps[currentIndex].state === "failed" ? Theme.red : Theme.accent; border.width: 1
        Column {
            anchors.centerIn: parent; width: 110; spacing: 5
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: bridge.steps.length ? "STEP " + (wheel.currentIndex + 1) : "READY"; color: Theme.accent; font.family: Theme.mono; font.pixelSize: 9 }
            Text { anchors.horizontalCenter: parent.horizontalCenter; width: parent.width; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight; text: bridge.steps.length ? bridge.steps[wheel.currentIndex].action.split(":")[0].toUpperCase() : "JARVIS"; color: Theme.white; font.family: Theme.sans; font.bold: true; font.pixelSize: 14 }
            Text { anchors.horizontalCenter: parent.horizontalCenter; width: parent.width; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideMiddle; text: bridge.steps.length ? bridge.steps[wheel.currentIndex].message || bridge.steps[wheel.currentIndex].action.split(":").slice(1).join(":") : "CORE"; color: Theme.muted; font.family: Theme.mono; font.pixelSize: 8 }
        }
    }
}
