// Unifideck's xCloud player page.
//
// Streams one xCloud title with Greenlight's MIT player library (src/lib,
// see ../LICENSE-greenlight-player). Every xCloud call goes through the
// launcher's local server (launcher/xcloud_player/server.py), which holds the
// tokens: this page never sees one. Built into
// py_modules/unifideck/launcher/xcloud_player/web/bundle.js by ../build.sh.
import xCloudPlayer from './lib/player'
import Gamepad from './lib/input/gamepad'

const statusEl = document.getElementById('status') as HTMLElement
let shown = ''
const show = (msg: string, error = false) => {
    statusEl.textContent = msg
    statusEl.className = error ? 'error' : ''
    statusEl.style.display = msg ? 'block' : 'none'
    if (msg && msg !== shown) void fetch('/api/log', { method: 'POST', body: (error ? 'ERROR ' : '') + msg })
    shown = msg
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

async function api(path: string, body?: unknown): Promise<any> {
    const r = await fetch('/api/' + path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(j.message || `${path} failed (HTTP ${r.status})`)
    return j
}

async function waitForConsole() {
    let connected = false
    for (;;) {
        const s = await api('state')
        if (s.state === 'Provisioned') return
        if (s.state === 'Failed') throw new Error(s.message || 'Xbox Cloud Gaming could not start this game')
        if (s.state === 'WaitingForResources') show('Waiting in the Xbox Cloud Gaming queue…')
        else show('Starting your game…')
        if (s.state === 'ReadyToConnect' && !connected) {
            connected = true
            await api('connect', {})
        }
        await sleep(1000)
    }
}

async function waitForHandshake(player: xCloudPlayer) {
    const channels = (player as any)._channels
    for (let i = 0; i < 300 && !channels.message?._handshakeCompleted; i++) await sleep(100)
    if (!channels.message?._handshakeCompleted) {
        void fetch('/api/log', { method: 'POST', body: 'ERROR stream handshake never completed' })
    }
    await sleep(500) // let sendAuthorization's add/remove reach the console first
}

async function run() {
    show('Starting your game…')
    await api('start', {})
    await waitForConsole()

    const player = new xCloudPlayer('playerContainer')
    player.init()
    const offer = await player.createOffer()
    const sdp = await api('sdp', { sdp: offer.sdp })
    await player.setRemoteSDP(JSON.parse(sdp.exchangeResponse).sdp)

    const local = (await player.getICECandidates()).map((c: RTCIceCandidate) => JSON.stringify({
        candidate: c.candidate, sdpMid: c.sdpMid, sdpMLineIndex: c.sdpMLineIndex,
        usernameFragment: c.usernameFragment,
    }))
    const ice = await api('ice', { candidates: local })
    await player.setRemoteIceCandidates(JSON.parse(ice.exchangeResponse))

    // The gamepad must be attached AFTER the message channel's handshake: on
    // HandshakeAck the library authorizes control and sends "pad 0 added" then
    // "pad 0 removed", expecting the app to attach its own pad afterwards. An
    // earlier attach is undone by that "removed" (its own "added" is dropped
    // while the control channel is still opening), so the console ignores all
    // input although the page sees the controller. Measured 2026-11-03.
    await waitForHandshake(player)
    const pad = new Gamepad(0, { enable_keyboard: true })
    pad.attach(player)
    void fetch('/api/log', { method: 'POST', body: 'controller attached' })
    setInterval(() => void api('keepalive', {}).catch(() => undefined), 30_000)
    show('')
    void fetch('/api/log', { method: 'POST', body: 'streaming' })

    // One line per change of what the Gamepad API reports, for the launch log.
    // Chromium exposes a pad only after its first button press, which is
    // usually after attach() above; when its `gamepadconnected` does not
    // reach the handler the pad is never bound and the stream ignores all
    // input, so bind any pad that is visible but not yet in use.
    let last = ''
    setInterval(() => {
        if (pad.getPhysicalGamepadId() < 0 && [...navigator.getGamepads()].some(Boolean)) {
            pad.detectActiveGamepad()
        }
        const pads = [...navigator.getGamepads()].filter(Boolean).map((p) => `${p!.id} (${p!.mapping || 'no mapping'})`)
        const now = pads.join(', ') || 'none'
        if (now !== last) { last = now; void fetch('/api/log', { method: 'POST', body: 'gamepads: ' + now }) }
    }, 1000)
}

run().catch((e) => show(String(e?.message ?? e), true))
