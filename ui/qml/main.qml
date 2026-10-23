import QtQuick
import QtQuick.Window
import QtQuick.Controls
import "."

Window {
    id: coreWindow
    objectName: "coreWindow"
    width: 460 * Theme.uiScale
    height: 460 * Theme.uiScale
    visible: bridge.visible
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput

    property real spin: 0
    property real lightAngle: 0
    property real targetLightAngle: 0
    property real audioLevel: bridge.level
    property color stateColor: bridge.state === "CONFIRMING" ? Theme.amber : bridge.state === "ERROR" ? Theme.red : Theme.accent

    Behavior on lightAngle { SpringAnimation { spring: 5.2; damping: 0.36; epsilon: 0.02 } }

    Timer {
        interval: Theme.reduceMotion ? 100 : 16
        running: bridge.visible && bridge.state !== "SLEEPING" && !Theme.reduceMotion
        repeat: true
        onTriggered: {
            coreWindow.spin = (coreWindow.spin + (bridge.state === "THINKING" ? 2.3 : 0.45)) % 360
            coreCanvas.requestPaint()
        }
    }
    Timer {
        interval: 100
        running: bridge.visible && bridge.state === "SLEEPING" && !Theme.reduceMotion
        repeat: true
        onTriggered: { coreWindow.spin = (coreWindow.spin + 0.25) % 360; coreCanvas.requestPaint() }
    }
    Connections {
        target: bridge
        function onChanged() {
            coreWindow.targetLightAngle += 137.5
            coreWindow.lightAngle = coreWindow.targetLightAngle
            coreWindow.audioLevel = bridge.level
            coreCanvas.requestPaint()
        }
    }

    Canvas {
        id: coreCanvas
        anchors.fill: parent
        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            var cx = width / 2, cy = height / 2, unit = width / 460
            var pulse = bridge.state === "LISTENING" ? 1 + audioLevel * 0.16 : 1 + Math.sin(spin * Math.PI / 180) * 0.025
            var c = coreWindow.stateColor
            ctx.save(); ctx.translate(cx, cy); ctx.scale(pulse, pulse)
            // Low-opacity hex grid and fine HUD ticks.
            ctx.strokeStyle = Qt.alpha(c, 0.035); ctx.lineWidth = 1
            for (var gy = -190; gy <= 190; gy += 32) {
                for (var gx = -190; gx <= 190; gx += 37) {
                    ctx.beginPath(); ctx.moveTo(gx, gy - 9); ctx.lineTo(gx + 8, gy - 4); ctx.lineTo(gx + 8, gy + 5); ctx.lineTo(gx, gy + 10); ctx.lineTo(gx - 8, gy + 5); ctx.lineTo(gx - 8, gy - 4); ctx.closePath(); ctx.stroke()
                }
            }
            for (var ring = 0; ring < 4; ring++) {
                var radius = (88 + ring * 23) * unit
                ctx.beginPath(); ctx.arc(0, 0, radius, 0, Math.PI * 2)
                ctx.strokeStyle = Qt.alpha(c, 0.15 + ring * 0.035); ctx.lineWidth = 1; ctx.stroke()
                ctx.save(); ctx.rotate((spin * (ring % 2 ? -1.2 : 1.0) + ring * 28) * Math.PI / 180)
                for (var tick = 0; tick < 48; tick++) {
                    var angle = tick * Math.PI * 2 / 48
                    var inner = radius - (tick % 4 === 0 ? 8 : 3)
                    ctx.beginPath(); ctx.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner); ctx.lineTo(Math.cos(angle) * radius, Math.sin(angle) * radius)
                    ctx.strokeStyle = Qt.alpha(c, tick % 4 === 0 ? 0.68 : 0.26); ctx.stroke()
                }
                ctx.restore()
            }
            // Spring-following, three-pass rim light arc.
            var a = lightAngle * Math.PI / 180
            for (var layer = 0; layer < 3; layer++) {
                ctx.beginPath(); ctx.arc(0, 0, 178 * unit, a, a + 0.9)
                ctx.strokeStyle = Qt.alpha(c, [0.10, 0.22, 0.85][layer]); ctx.lineWidth = [18, 8, 2][layer] * unit; ctx.stroke()
            }
            // Reactive radial bars around the perimeter.
            for (var bar = 0; bar < 54; bar++) {
                var theta = bar * Math.PI * 2 / 54
                var level = bridge.state === "LISTENING" || bridge.state === "SPEAKING" ? audioLevel * (0.4 + 0.6 * Math.abs(Math.sin(bar * 1.7 + spin / 11))) : 0.06
                var length = (5 + 24 * level) * unit
                ctx.beginPath(); ctx.moveTo(Math.cos(theta) * 202 * unit, Math.sin(theta) * 202 * unit); ctx.lineTo(Math.cos(theta) * (202 * unit + length), Math.sin(theta) * (202 * unit + length))
                ctx.lineWidth = (bar % 3 === 0 ? 2 : 1) * unit; ctx.strokeStyle = Qt.alpha(c, 0.32 + level * 0.62); ctx.stroke()
            }
            if (bridge.state === "EXECUTING" && bridge.steps.length) {
                var done = 0
                for (var step = 0; step < bridge.steps.length; step++) if (bridge.steps[step].state === "done") done++
                ctx.beginPath(); ctx.arc(0, 0, 188 * unit, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * done / bridge.steps.length)
                ctx.strokeStyle = Qt.alpha(c, 0.88); ctx.lineWidth = 2 * unit; ctx.stroke()
            }
            var glow = ctx.createRadialGradient(0, 0, 4, 0, 0, 83 * unit)
            glow.addColorStop(0, Qt.alpha(c, 0.29)); glow.addColorStop(0.55, Qt.alpha(c, 0.10)); glow.addColorStop(1, Qt.alpha(c, 0))
            ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(0, 0, 83 * unit, 0, Math.PI * 2); ctx.fill()
            ctx.beginPath(); ctx.arc(0, 0, 47 * unit, 0, Math.PI * 2); ctx.fillStyle = Qt.alpha(c, 0.10); ctx.fill(); ctx.strokeStyle = Qt.alpha(c, 0.7); ctx.lineWidth = 1; ctx.stroke()
            ctx.restore()
        }
    }

    Column {
        anchors.centerIn: parent
        spacing: 7
        Text { anchors.horizontalCenter: parent.horizontalCenter; text: bridge.state; color: coreWindow.stateColor; font.family: Theme.mono; font.pixelSize: 12 * Theme.uiScale; font.letterSpacing: 3 }
        Text { anchors.horizontalCenter: parent.horizontalCenter; width: 260 * Theme.uiScale; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight; text: bridge.state === "LISTENING" ? "LISTENING" : bridge.heard || "JARVIS CORE"; color: Theme.white; font.family: Theme.sans; font.pixelSize: 14 * Theme.uiScale }
        Text { visible: bridge.state === "CONFIRMING"; anchors.horizontalCenter: parent.horizontalCenter; text: "SAY YES / NO"; color: Theme.amber; font.family: Theme.mono; font.pixelSize: 10 * Theme.uiScale }
    }
    ChainWheel { anchors.fill: parent; visible: bridge.steps.length > 0 }
Window {
    id: pillWindow
    objectName: "pillWindow"
    width: 430 * Theme.uiScale; height: 72 * Theme.uiScale
    visible: bridge.visible && bridge.detail.length > 0
    property real pillOpacity: 0
    property real pillOffset: 18
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput
    Timer { id: pillHide; interval: 1400; onTriggered: pillWindow.pillOpacity = 0 }
    Behavior on pillOffset { SpringAnimation { spring: 9; damping: 0.42 } }
    Connections {
        target: bridge
        function onChanged() {
            pillWindow.pillOpacity = 1
            pillWindow.pillOffset = 0
            pillHide.restart()
        }
    }
    Rectangle {
        anchors.fill: parent; radius: 12; color: Theme.glass; border.color: bridge.state === "ERROR" || bridge.error.length ? Theme.red : bridge.state === "CONFIRMING" ? Theme.amber : Theme.accent; border.width: 1; opacity: pillWindow.pillOpacity
        transform: Translate { y: pillWindow.pillOffset }
        Behavior on opacity { NumberAnimation { duration: 220 } }
        Row {
            anchors.fill: parent; anchors.margins: 14; spacing: 13
            Rectangle { width: 34; height: 34; radius: 17; color: Qt.alpha(Theme.accent, 0.15); border.color: Theme.accent; Text { anchors.centerIn: parent; text: bridge.state === "ERROR" ? "×" : "◈"; color: Theme.accent; font.pixelSize: 19 } }
            Column {
                width: parent.width - 62; spacing: 5
                Text { text: "JARVIS  /  " + bridge.state; color: Theme.accent; font.family: Theme.mono; font.pixelSize: 10 }
                Text { width: parent.width; text: bridge.detail; elide: Text.ElideRight; color: Theme.white; font.family: Theme.sans; font.pixelSize: 12 }
        Rectangle { width: 150; height: 3; radius: 2; color: Qt.alpha(Theme.accent, 0.22); Rectangle { width: parent.width * bridge.level; height: parent.height; radius: 2; color: Theme.accent; Behavior on width { SpringAnimation { spring: 9; damping: 0.45 } } } }
            }
        }
    }
}

WorkspacePanel { }
DebugOverlay { }
}
