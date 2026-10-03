import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Live Claude Code sessions. Data comes from claude_sessions.py
// (reads ~/.claude/sessions and the session transcripts).
Panel {
  id: root
  moduleName: "edward-kulyk.claude-sessions"
  ipcTarget: "edward-kulyk.claude-sessions"
  manageIpc: false

  property var sessions: []
  property int now: Math.floor(Date.now() / 1000)
  property int frame: 0
  property real spinAngle: 0
  property int cursor: 0
  property bool cursorActive: false

  readonly property string helper: Qt.resolvedUrl("claude_sessions.py").toString().replace(/^file:\/\//, "")
  readonly property int waiting: Model.waitingCount(sessions)
  readonly property bool anyBusy: Model.busyCount(sessions) > 0
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property int pollMs: Math.max(500, parseInt(setting("pollMs", 1500), 10) || 1500)

  // Contextual visibility: the icon only takes bar space when it has something
  // to say, while its popup is open (summoned by hotkey/menu via IPC), or — for
  // centre widgets — while the centre section is hovered (like the indicators).
  property bool summoned: false
  readonly property bool contextual: String(setting("contextual", true)) !== "false"
  readonly property bool reveal: false
  readonly property bool shown: !contextual || opened || summoned || reveal || root.sessions.length > 0
  function summon() { summoned = true; Qt.callLater(function() { root.open() }) }
  visible: shown
  implicitWidth: shown ? button.implicitWidth : 0
  Connections {
    target: root
    function onOpenedChanged() { if (!root.opened) root.summoned = false }
  }
  implicitHeight: button.implicitHeight

  function refresh() { if (!listProcess.running) listProcess.running = true }

  function focusSession(s) {
    if (!s) return
    focusProcess.command = ["python3", helper, "focus", String(s.pid)]
    focusProcess.running = true
    close()
  }

  function focusLongestWaiting() {
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].status === "waiting") { focusSession(sessions[i]); return }
    }
    toggle()
  }

  onOpenedChanged: if (opened) {
    cursorActive = false
    cursor = 0
    refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }
  onSessionsChanged: if (cursor >= sessions.length) cursor = Math.max(0, sessions.length - 1)

  Process {
    id: listProcess
    command: ["python3", root.helper, "list"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var d = Model.parse(text)
        if (!d) return
        root.sessions = d.sessions
        root.now = d.now || Math.floor(Date.now() / 1000)
      }
    }
  }

  Process { id: focusProcess; command: [] }

  Timer { interval: root.pollMs; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.refresh() }
  // Spin the bar 󰛄 while every session is working; settle upright otherwise.
  NumberAnimation on spinAngle {
    from: 0; to: 360; duration: 2400
    loops: Animation.Infinite
    running: root.anyBusy && root.waiting === 0
    onRunningChanged: if (!running) root.spinAngle = 0
  }
  Timer {
    interval: 150
    running: root.opened && root.anyBusy
    repeat: true
    onTriggered: root.frame = (root.frame + 1) % Model.SPINNER.length
  }
  Timer {
    interval: 1000
    running: root.opened
    repeat: true
    onTriggered: root.now = Math.floor(Date.now() / 1000)
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.summon() }
    function close(): void { root.close() }
    function toggle(): void { if (root.opened) root.close(); else root.summon() }
    function focusWaiting(): void { root.focusLongestWaiting() }
    function status(): string { return Model.tooltip(root.sessions, root.now) }
  }

  // Label is drawn by our own Row so the 󰛄 can spin without moving the count;
  // the button's invisible text only sizes the slot.
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰛄" + (Model.countText(root.sessions) ? " " + Model.countText(root.sessions) : "")
    labelVisible: false
    fontSize: Style.font.body
    horizontalMargin: 6
    verticalPadding: 6
    fixedHeight: root.bar ? root.bar.barSize : -1
    dimmed: root.sessions.length === 0
    tooltipText: Model.tooltip(root.sessions, root.now)
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.focusLongestWaiting()
      else root.toggle()
    }

    Row {
      anchors.centerIn: parent
      spacing: Math.round(glyphMetrics.averageCharacterWidth)
      readonly property color tint: root.waiting > 0 ? root.accent : button.foreground

      Text {
        id: star
        text: "󰛄"
        color: parent.tint
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        anchors.verticalCenter: parent.verticalCenter
        rotation: root.spinAngle
      }
      Text {
        text: Model.countText(root.sessions)
        visible: text !== ""
        color: parent.tint
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        anchors.verticalCenter: parent.verticalCenter
      }
    }
    FontMetrics { id: glyphMetrics; font.family: button.fontFamily; font.pixelSize: button.fontSize }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0 && root.sessions.length > 0)
          root.cursor = Math.max(0, Math.min(root.sessions.length - 1, root.cursor + dy))
      }
      onActivateRequested: if (root.cursorActive) root.focusSession(root.sessions[root.cursor])
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { if (t === "r" || t === "R") root.refresh() }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "Claude"
            meta: Model.heroMeta(root.sessions)
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.sessions.length > 0 ? 1.0 : 0.5
            iconComponent: Component {
              Text {
                text: root.waiting > 0 || !root.anyBusy ? "󰛄" : Model.SPINNER[root.frame]
                color: root.waiting > 0 ? root.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          PanelSeparator { foreground: root.foreground; visible: root.sessions.length > 0 }

          Column {
            id: rows
            width: parent.width
            spacing: Style.space(6)
            visible: root.sessions.length > 0

            PanelSectionHeader {
              text: "SESSIONS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.sessions
              SessionRow {
                required property var modelData
                required property int index
                width: rows.width
                session: modelData
                rowIndex: index
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: "Enter jump to terminal · right-click icon: next waiting"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }
        }
      }
    }
  }

  component SessionRow: CursorSurface {
    id: row
    property var session: null
    property int rowIndex: 0
    readonly property bool isWaiting: session && session.status === "waiting"

    hasCursor: root.cursorActive && root.cursor === rowIndex
    current: isWaiting
    foreground: root.foreground
    implicitHeight: content.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: { root.cursorActive = true; root.cursor = row.rowIndex }
      onClicked: root.focusSession(row.session)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      Text {
        text: row.isWaiting ? "●" : Model.SPINNER[(root.frame + row.rowIndex * 3) % Model.SPINNER.length]
        color: row.isWaiting ? root.accent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignTop
        Layout.topMargin: Style.space(1)
      }

      ColumnLayout {
        id: content
        Layout.fillWidth: true
        spacing: Style.space(2)
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: row.session ? Model.sessionTitle(row.session) : ""
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: row.session ? Model.sessionMeta(row.session, root.now) : ""
          color: row.isWaiting ? root.accent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          visible: row.session && row.session.lastPrompt !== ""
          text: row.session ? "› " + row.session.lastPrompt.replace(/\s+/g, " ") : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.italic: true
          elide: Text.ElideRight
        }
      }
    }
  }
}
