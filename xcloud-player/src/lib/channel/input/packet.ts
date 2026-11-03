// import { InputFrame, PointerFrame, MouseFrame, KeyboardFrame } from '../Input'

export interface InputFrame {
    GamepadIndex: number;
    Nexus: number;
    Menu: number;
    View: number;
    A: number;
    B: number;
    X: number;
    Y: number;
    DPadUp: number;
    DPadDown: number;
    DPadLeft: number;
    DPadRight: number;
    LeftShoulder: number;
    RightShoulder: number;
    LeftThumb: number;
    RightThumb: number;

    LeftThumbXAxis: number;
    LeftThumbYAxis: number;
    RightThumbXAxis: number;
    RightThumbYAxis: number;
    LeftTrigger: number;
    RightTrigger: number;
}

export interface PointerFrame {
    events: Array<any>;
}

export interface MouseFrame {
    X: number;
    Y: number;
    WheelX: number;
    WheelY: number;
    Buttons: number;
    Relative: number;
}

export interface KeyboardFrame {
    pressed: boolean;
    keyCode: number;
    key: string;
}

export enum ReportTypes {
    None = 0,
    Metadata = 1,
    Gamepad = 2,
    Pointer = 4,
    ClientMetadata = 8,
    ServerMetadata = 16,
    Mouse = 32,
    Keyboard = 64,
    Vibration = 128,
    Sensor = 256,
    UnreliableInputReport = 512,
    UnreliableInputAck = 1024,
}

export interface MetadataFrame {
    serverDataKey: number;
    firstFramePacketArrivalTimeMs: number;
    frameSubmittedTimeMs: number;
    frameDecodedTimeMs: number;
    frameRenderedTimeMs: number;
}

export interface StreamConfig {
    width: number;
    height: number;
}

export default class InputPacket {

    _reportType = ReportTypes.None
    _totalSize = -1
    _sequence = -1

    _metadataFrames:Array<MetadataFrame> = []
    _gamepadFrames:Array<InputFrame> = []
    _pointerFrames:Array<PointerFrame> = []
    _mouseFrames:Array<MouseFrame> = []
    _keyboardFrames:Array<KeyboardFrame> = []

    _maxTouchpoints = 0
    _serverVideoWidth = 0
    _serverVideoHeight = 0

    constructor(sequence:number){
        this._sequence = sequence
    }

    setStreamConfig(options:StreamConfig){
        this._serverVideoWidth = options.width
        this._serverVideoHeight = options.height
    }

    setMetadata(maxTouchpoints = 1){
        this._reportType = ReportTypes.ClientMetadata
        this._totalSize = 15
        this._maxTouchpoints = maxTouchpoints
    }

    setData(metadataQueue:Array<any>, gamepadQueue:Array<InputFrame>, pointerQueue:Array<PointerFrame>, mouseQueue:Array<MouseFrame>, keyboardQueue:Array<KeyboardFrame>){
        let size = 14

        if(metadataQueue.length > 0){
            this._reportType |= ReportTypes.Metadata
            size = size+this._calculateMetadataSize(metadataQueue)
            this._metadataFrames = metadataQueue
        }

        if(gamepadQueue.length > 0){
            this._reportType |= ReportTypes.Gamepad
            size = size+this._calculateGamepadSize(gamepadQueue)
            this._gamepadFrames = gamepadQueue
        }

        if(pointerQueue.length > 0){
            this._reportType |= ReportTypes.Pointer
            size = size+this._calculatePointerSize(pointerQueue)
            this._pointerFrames = pointerQueue
        }

        if(mouseQueue.length > 0){
            this._reportType |= ReportTypes.Mouse
            size = size+this._calculateMouseSize(mouseQueue)
            this._mouseFrames = mouseQueue
        }

        if(keyboardQueue.length > 0){
            this._reportType |= ReportTypes.Keyboard
            size = size+this._calculateKeyboardSize(keyboardQueue)
            this._keyboardFrames = keyboardQueue
        }

        this._totalSize = size
    }

    _calculateMetadataSize(frames:Array<MetadataFrame>){
        return 1 + ((7 * 4) * frames.length)
    }

    _calculateGamepadSize(frames:Array<InputFrame>){
        return 1 + (23 * frames.length)
    }

    _calculatePointerSize(frames:Array<PointerFrame>){
        let pointerSize = 1
        for(const frame in frames){
            pointerSize = pointerSize + 1 + (frames[frame].events.length * 20)
        }

        return pointerSize
    }

    _calculateMouseSize(frames:Array<MouseFrame>){
        return 1 + (18 * frames.length)
    }

    _calculateKeyboardSize(frames:Array<KeyboardFrame>){
        return 1 + (3 * frames.length)
    }



    _writeMetadataData(packet:DataView, offset:number, frames:Array<MetadataFrame>){
        packet.setUint8(offset, frames.length)
        offset++

        if(frames.length >= 30){
            console.warn('metadataQueue is bigger then 30. This might impact reliability!')
        }

        for(let i = 0; i < frames.length; i++) {
            const frame = frames[i]

            const firstFramePacketArrivalTimeMs = frame.firstFramePacketArrivalTimeMs
            const frameSubmittedTimeMs = frame.frameSubmittedTimeMs
            const frameDecodedTimeMs = frame.frameDecodedTimeMs
            const frameRenderedTimeMs = frame.frameRenderedTimeMs
            const framePacketTime = performance.now()
            const frameDateNow = performance.now()

            packet.setUint32(offset, frame.serverDataKey, true)
            packet.setUint32(offset+4, firstFramePacketArrivalTimeMs, true)
            packet.setUint32(offset+8, frameSubmittedTimeMs, true)
            packet.setUint32(offset+12, frameDecodedTimeMs, true)
            packet.setUint32(offset+16, frameRenderedTimeMs, true)
            packet.setUint32(offset+20, framePacketTime, true)
            packet.setUint32(offset+24, frameDateNow, true)

            offset += 28
        }

        return offset
    }

    _writeGamepadData(packet:DataView, offset:number, frames:Array<InputFrame>){
        packet.setUint8(offset, frames.length)
        offset++

        if(frames.length >= 30){
            console.warn('gamepadQueue is bigger then 30. This might impact reliability!')
        }

        for(let i = 0; i < frames.length; i++) {
            const input:InputFrame = frames[i]

            packet.setUint8(offset, input.GamepadIndex)
            offset++

            let buttonMask = 0
            if(input.Nexus > 0){ buttonMask |= 2 }
            if(input.Menu > 0){ buttonMask |= 4 }
            if(input.View > 0){ buttonMask |= 8 }
            if(input.A > 0){ buttonMask |= 16 }
            if(input.B > 0){ buttonMask |= 32 }
            if(input.X > 0){ buttonMask |= 64 }
            if(input.Y > 0){ buttonMask |= 128 }
            if(input.DPadUp > 0){ buttonMask |= 256 }
            if(input.DPadDown > 0){ buttonMask |= 512 }
            if(input.DPadLeft > 0){ buttonMask |= 1024 }
            if(input.DPadRight > 0){ buttonMask |= 2048 }
            if(input.LeftShoulder > 0){ buttonMask |= 4096 }
            if(input.RightShoulder > 0){ buttonMask |= 8192 }
            if(input.LeftThumb > 0){ buttonMask |= 16384 }
            if(input.RightThumb > 0){ buttonMask |= 32768 }

            packet.setUint16(offset, buttonMask, true)
            packet.setInt16(offset+2, this._normalizeAxisValue(input.LeftThumbXAxis), true) // LeftThumbXAxis
            packet.setInt16(offset+4, this._normalizeAxisValue(-input.LeftThumbYAxis), true) // LeftThumbYAxis
            packet.setInt16(offset+6, this._normalizeAxisValue(input.RightThumbXAxis), true) // RightThumbXAxis
            packet.setInt16(offset+8, this._normalizeAxisValue(-input.RightThumbYAxis), true) // RightThumbYAxis
            packet.setUint16(offset+10, this._normalizeTriggerValue(input.LeftTrigger), true) // LeftTrigger
            packet.setUint16(offset+12, this._normalizeTriggerValue(input.RightTrigger), true) // RightTrigger

            packet.setUint32(offset+14, 1, true) // PhysicalPhysicality
            packet.setUint32(offset+18, 1, false) // VirtualPhysicality
            offset += 22
        }

        return offset
    }

    _writePointerData(packet:DataView, offset:number, frames:Array<PointerFrame>){
        packet.setUint8(offset, 1)
        // packet.setUint8(offset, frames.length)
        offset++

        if(frames.length >= 2){
            console.warn('pointerQueue is bigger then 1. Only one event will be sent.')
        }

        // for (; frames.length > 0;) {
        // this._inputFps.count()
        const frame = frames[0]
        if(frame !== undefined){
            packet.setUint8(offset, frame.events.length)
            offset++

            const screenWidth = this._serverVideoWidth
            const screenHeight = this._serverVideoHeight

            for(let i = 0; i < frame.events.length; i++){
                const event = frame.events[i]
                const rect = event.target.getBoundingClientRect()

                let e = 0.06575749909301447 * (screenHeight / 1)
                let n = 0.06575749909301447 * (screenWidth / 1)

                // @TODO: Check if this is correct
                e =1
                n =1

                if(event.type === 'pointerup'){
                    e = 0
                    n = 0
                }

                packet.setUint16(offset, e, true)
                packet.setUint16(offset+2, n, true)
                packet.setUint8(offset+4, 255*event.pressure)
                packet.setUint16(offset+5, event.twist, true)
                packet.setUint32(offset+7, 0, true)
                let o = (event.x - rect.left) * (screenWidth / rect.width)
                    , l = (event.y - rect.top) * (screenHeight / rect.height)

                if(event.type === 'pointerup'){
                    // Reset x and y to 0 on pointerup
                    o = 0
                    l = 0
                }

                packet.setUint32(offset+11, o, true)
                packet.setUint32(offset+15, l, true)
                packet.setUint8(offset+19, (event.type === 'pointerdown') ? 1 :
                    (event.type === 'pointerup') ? 2 : (event.type === 'pointermove') ? 3 : 0)
                    
                offset = offset+20
            }
        }

        return offset
    }

    _writeMouseData(packet:DataView, offset:number, frames:Array<MouseFrame>){
        packet.setUint8(offset, frames.length)
        offset++

        if(frames.length >= 30){
            console.warn('mouseQueue is bigger then 30. This might impact reliability!')
        }

        for(let i = 0; i < frames.length; i++) {
            const input:MouseFrame = frames[i]

            packet.setUint32(offset, input.X, true)
            packet.setUint32(offset+4, input.Y, true)
            packet.setUint32(offset+8, input.WheelX, true)
            packet.setUint32(offset+12, input.WheelY, true)

            packet.setUint8(offset+16, input.Buttons)
            packet.setUint8(offset+17, input.Relative)
            offset += 18
        }

        return offset
    }

    _writeKeyboardData(packet:DataView, offset:number, frames:Array<KeyboardFrame>){
        packet.setUint8(offset, frames.length)
        offset++

        if(frames.length >= 30){
            console.warn('keyboardQueue is bigger then 30. This might impact reliability!')
        }

        for(let i = 0; i < frames.length; i++) {
            const input:KeyboardFrame = frames[i]

            packet.setUint8(offset, 2) // 1 = Known, 2 = VKey, 3 = AppCommand, 0 = Unknown
            packet.setUint8(offset+1, input.pressed ? 1 : 0)
            packet.setUint8(offset+2, input.keyCode)
            // packet.setUint8(offset+3, 1)
            // packet.setUint8(offset+4, input.keyCode)

            offset += 3
        }

        return offset
    }

    toBuffer(){
        const metadataAlloc = new Uint8Array(this._totalSize)
        const packet = new DataView(metadataAlloc.buffer)

        packet.setUint16(0, this._reportType, true)
        packet.setUint32(2, this._sequence, true)
        packet.setFloat64(6, performance.now(), true)

        let offset = 14

        if(this._metadataFrames.length > 0) {offset = this._writeMetadataData(packet, offset, this._metadataFrames)}
        if(this._gamepadFrames.length > 0) {offset = this._writeGamepadData(packet, offset, this._gamepadFrames)}
        if(this._pointerFrames.length > 0) {offset = this._writePointerData(packet, offset, this._pointerFrames)}
        if(this._mouseFrames.length > 0) {offset = this._writeMouseData(packet, offset, this._mouseFrames)}
        if(this._keyboardFrames.length > 0) {offset = this._writeKeyboardData(packet, offset, this._keyboardFrames)}

        if(this._reportType === ReportTypes.ClientMetadata){
            packet.setUint8(offset, this._maxTouchpoints)
            offset++
        }

        if(offset !== offset){
            throw new Error('Packet length mismatch. Something is wrong!')
        }

        return packet
    }

    _normalizeTriggerValue(value: number): number {
        if (value <= 0 || Number.isNaN(value)) {
            return 0
        }

        if (value >= 1) {
            return 65535
        }

        // Fast float -> int conversion in range [0, 65535]
        return (value * 65535) | 0
    }

    _normalizeAxisValue(value: number) {
        const max = 32767
        const min = -32767
        const scaled = Math.round(value * max)
        return scaled > max ? max : scaled < min ? min : scaled
    }
}