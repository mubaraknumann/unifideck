import xCloudPlayer from '../../player'
import InputPacket, { InputFrame as GamepadFrame, KeyboardFrame, MetadataFrame, MouseFrame, PointerFrame } from './packet'

export default class InputQueue {
    private _player:xCloudPlayer

    private _inputSequenceNum = 0

    private _metadataQueue:Array<MetadataFrame> = []
    private _gamepadQueue:Array<GamepadFrame> = []
    private _pointerQueue:Array<PointerFrame> = []
    private _mouseQueue:Array<MouseFrame> = []
    private _keyboardQueue:Array<KeyboardFrame> = []

    // Axis change detection and heartbeat tracking
    private _gamepadHeartbeatTimers = new Map<number, number>()
    private _lastGamepadState = new Map<number, GamepadFrame>()
    private _axisChangeThreshold = 0.08

    constructor(player:xCloudPlayer) {
        this._player = player
    }

    private _hasQueuedData() {
        return this._metadataQueue.length > 0 ||
            this._gamepadQueue.length > 0 ||
            this._pointerQueue.length > 0 ||
            this._mouseQueue.length > 0 ||
            this._keyboardQueue.length > 0
    }

    private _upsertGamepadFrame(data:GamepadFrame) {
        for(let i = 0; i < this._gamepadQueue.length; i++){
            if(this._gamepadQueue[i].GamepadIndex === data.GamepadIndex){
                this._gamepadQueue[i] = data
                return
            }
        }

        this._gamepadQueue.push(data)
    }

    private _hasSignificantAxisChange(current:GamepadFrame, last:GamepadFrame | undefined):boolean {
        if(!last) return true  // First frame always send

        const stickNames = ['LeftThumbXAxis', 'LeftThumbYAxis', 'RightThumbXAxis', 'RightThumbYAxis'] as const
        const triggerNames = ['LeftTrigger', 'RightTrigger'] as const

        // For analog sticks: only skip if BOTH change is small AND value is near center
        for(const stick of stickNames){
            const delta = Math.abs(current[stick] - last[stick])
            const value = Math.abs(current[stick])
            if(delta > this._axisChangeThreshold || value > 0.15){
                return true
            }
        }

        // For triggers: always send if they changed (higher sensitivity needed)
        for(const trigger of triggerNames){
            if(Math.abs(current[trigger] - last[trigger]) > 0.01){
                return true
            }
        }

        return false
    }

    private _isHeartbeatDue(gamepadIndex:number):boolean {
        const lastHeartbeat = this._gamepadHeartbeatTimers.get(gamepadIndex) ?? 0
        return performance.now() - lastHeartbeat > 33  // Send at least every 33ms
    }

    queueMetadataFrame(data:MetadataFrame) {
        this._metadataQueue.push(data)
        this.checkQueueAndSend()
    }

    queueGamepadFrame(data:GamepadFrame) {
        const lastFrame = this._lastGamepadState.get(data.GamepadIndex)
        const hasChange = this._hasSignificantAxisChange(data, lastFrame)
        const isDue = this._isHeartbeatDue(data.GamepadIndex)

        if(hasChange || isDue){
            this._upsertGamepadFrame(data)
            this._lastGamepadState.set(data.GamepadIndex, data)
            this._gamepadHeartbeatTimers.set(data.GamepadIndex, performance.now())
            this.checkQueueAndSend()
        }
    }

    queueGamepadFrames(frames:Array<GamepadFrame>, forceSend = false) {
        for(const frame of frames){
            this._upsertGamepadFrame(frame)
        }
        this.checkQueueAndSend(forceSend)
    }

    queueMouseFrame(data:MouseFrame) {
        this._mouseQueue.push(data)
        this.checkQueueAndSend()
    }

    queueKeyboardFrame(data:KeyboardFrame) {
        this._keyboardQueue.push(data)
        this.checkQueueAndSend()
    }

    queuePointerFrame(data:PointerFrame) {
        if(this._pointerQueue.length > 0){
            this._pointerQueue[0].events = this._pointerQueue[0].events.concat(data.events)
        } else {
            this._pointerQueue.push(data)
        }
        this.checkQueueAndSend()
    }

    checkQueueAndSend(forceSend = false) {
        if(forceSend === true){
            if(this._hasQueuedData()){
                this.sendQueue()
            }
            return
        }

        // Prioritize low-latency gamepad delivery.
        if(this._gamepadQueue.length > 0){
            this.sendQueue()
            return
        }

        // Non-metadata input should also not wait.
        if(this._mouseQueue.length > 0 || this._keyboardQueue.length > 0 || this._pointerQueue.length > 0){
            this.sendQueue()
            return
        }

        // Metadata-only can batch slightly.
        if(this._metadataQueue.length > 5){
            this.sendQueue()
        }
    }

    sendQueue() {
        // console.log('Sending queues:', this._metadataQueue.length, this._gamepadQueue.length, this._pointerQueue.length, this._mouseQueue.length, this._keyboardQueue.length)

        const packet = new InputPacket(this.getSequenceNum())
        if(this._pointerQueue.length > 0){
            packet.setStreamConfig({
                width: this._player._channels.input.getServerVideoWidth(),
                height: this._player._channels.input.getServerVideoHeight(),
            })
        }
        packet.setData(this._metadataQueue, this._gamepadQueue, this._pointerQueue, this._mouseQueue, this._keyboardQueue)
        this._player._channels.input.send(packet.toBuffer())

        this.clearQueues()
    }

    clearQueues() {
        this._metadataQueue = []
        this._gamepadQueue = []
        this._pointerQueue = []
        this._mouseQueue = []
        this._keyboardQueue = []
    }

    getSequenceNum() {
        this._inputSequenceNum++
        return this._inputSequenceNum
    }
}