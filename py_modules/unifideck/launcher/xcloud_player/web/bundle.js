"use strict";
(() => {
  // xcloud-player/src/lib/channel/channel.ts
  var Channel = class {
    constructor(player) {
      this._player = player;
      this._dataChannel = this._player._peerConnection.createDataChannel(this.getChannelName(), this.getChannelConfig());
      this._dataChannel.onopen = this.onOpen.bind(this);
      this._dataChannel.onmessage = this.onMessage.bind(this);
      this._dataChannel.onclosing = this.onClosing.bind(this);
      this._dataChannel.onclose = this.onClose.bind(this);
      this._dataChannel.onerror = this.onError.bind(this);
    }
    // Channel config functions
    getChannelName() {
      return "channel";
    }
    getChannelConfig() {
      return {};
    }
    // Default channel functions
    onOpen(event) {
      console.log(this.getChannelName(), "Opening channel:", event);
    }
    onMessage(event) {
      console.log(this.getChannelName(), "Message channel:", event);
    }
    onClosing(event) {
      console.log(this.getChannelName(), "Closing channel:", event);
    }
    onClose(event) {
      console.log(this.getChannelName(), "Closed channel:", event);
    }
    onError(event) {
      console.log(this.getChannelName(), "Error channel:", event);
    }
    send(data) {
      if (this._dataChannel.readyState !== "open") {
        console.log("[" + this.getChannelName() + "] Tried to send data while channel is not open. readyState is:", this._dataChannel.readyState);
        return;
      }
      if (typeof data === "string") {
        data = new TextEncoder().encode(data);
      }
      return this._dataChannel.send(data);
    }
    getPlayer() {
      return this._player;
    }
    // Channel destroy function
    destroy() {
    }
  };

  // xcloud-player/src/lib/channel/chat.ts
  var ChatChannel = class extends Channel {
    constructor(player) {
      super(player);
      this._micPermissions = "prompt";
      navigator.permissions.query({ name: "microphone" }).then((permissionStatus) => {
        this._micPermissions = permissionStatus.state;
        permissionStatus.onchange = () => {
          this._micPermissions = permissionStatus.state;
        };
      });
    }
    getChannelName() {
      return "chat";
    }
    getChannelConfig() {
      return {
        ordered: true,
        protocol: "chatV1"
      };
    }
    // startMicrophone() {
    //     if(this._micPermissions === 'denied'){
    //         alert('Microphone permissions are denied. Please enable them in your browser settings.')
    //         return
    //     }
    //     if(this._micStream !== undefined){
    //         console.log('Microphone already started. You need to stop it first by calling stopMicrophone()')
    //         return
    //     }
    //     navigator.mediaDevices.getUserMedia({ audio: {
    //         channelCount: 1,
    //         sampleRate: 24e3,
    //     } }).then((stream) => {
    //         if(this._micStream !== undefined){
    //             return
    //         }
    //         this._micStream = stream
    //         // Add to SDP
    //         this.getPlayer()._peerConnection.addTrack(stream.getAudioTracks()[0], stream)
    //         this.getPlayer().createOffer().then((offer) => {
    //             if(this.getPlayer()._sdpHandler){
    //                 this.getPlayer()._sdpHandler(offer)
    //             } else {
    //                 console.log('No SDP handler set. Set an SDP Handler via player.setChatSdpHandler()')
    //             }
    //         })
    //     }).catch((err) => {
    //         alert(`Error connecting to microphone: ${err}`)
    //     })
    // }
    // stopMicrophone() {
    //     if(this._micStream === undefined) {return}
    //     this._micStream.getTracks().forEach((track) => {
    //         track.stop()
    //     })
    //     this.getPlayer()._peerConnection.getSenders().forEach((sender) => {
    //         if(sender.track && sender.track.kind === 'audio'){
    //             this.getPlayer()._peerConnection.removeTrack(sender)
    //         }
    //     })
    //     this._micStream = undefined
    // }
    destroy() {
    }
  };

  // xcloud-player/src/lib/input/gamepad.ts
  var Gamepad = class _Gamepad {
    constructor(index, options = {}) {
      this._physicalGamepadId = -1;
      this._isFocused = true;
      this._gamepadOverride = {
        buttons: {}
      };
      this._options = {
        enable_keyboard: false,
        enable_gamepad: true,
        enable_vibration: true,
        keyboard_mapping: {
          "A": "Enter",
          "B": "Backspace",
          "X": "x",
          "Y": "y",
          "DPadUp": "ArrowUp",
          "DPadDown": "ArrowDown",
          "DPadLeft": "ArrowLeft",
          "DPadRight": "ArrowRight",
          "LeftShoulder": "[",
          "RightShoulder": "]",
          "LeftThumb": "l",
          "RightThumb": "r",
          "LeftTrigger": "-",
          "RightTrigger": "=",
          "Menu": "m",
          "View": "v",
          "Nexus": "n"
        },
        gamepad_mapping: {
          "A": "0",
          "B": "1",
          "X": "2",
          "Y": "3",
          "DPadUp": "12",
          "DPadDown": "13",
          "DPadLeft": "14",
          "DPadRight": "15",
          "LeftShoulder": "4",
          "RightShoulder": "5",
          "LeftThumb": "10",
          "RightThumb": "11",
          "LeftTrigger": "6",
          "RightTrigger": "7",
          "Menu": "9",
          "View": "8",
          "Nexus": "16"
        },
        gamepad_axes_mapping: {
          "LeftThumbXAxis": "0",
          "LeftThumbYAxis": "1",
          "RightThumbXAxis": "2",
          "RightThumbYAxis": "3"
        },
        gamepad_deadzone: 0.08,
        gamepad_force_capture: false
      };
      this._listener = {
        keyDown: this.onKeyDown.bind(this),
        keyUp: this.onKeyUp.bind(this),
        gamepadConnected: this.onGamepadConnected.bind(this),
        gamepadDisconnected: this.onGamepadDisconnected.bind(this),
        windowFocus: this.onWindowFocus.bind(this),
        windowBlur: this.onWindowBlur.bind(this)
      };
      this._index = index;
      this._options = {
        ...this._options,
        ...options
      };
    }
    attach(xCloudPlayer2) {
      this._player = xCloudPlayer2;
      this._player._channels.control.sendGamepadState(this._index, true, this);
      if (this._options.enable_keyboard === true) {
        window.addEventListener("keydown", this._listener.keyDown);
        window.addEventListener("keyup", this._listener.keyUp);
      }
      if (this._options.enable_gamepad === true) {
        window.addEventListener("gamepadconnected", this._listener.gamepadConnected);
        window.addEventListener("gamepaddisconnected", this._listener.gamepadDisconnected);
      }
      if (this._options.enable_gamepad === true && this._options.gamepad_force_capture === false) {
        window.addEventListener("blur", this._listener.windowBlur);
        window.addEventListener("focus", this._listener.windowFocus);
      }
      this.detectActiveGamepad();
    }
    detach() {
      if (this._player === void 0) {
        console.log("[Gamepad] Player is not attached. this._player is:", this._player);
        return;
      }
      this._player._channels.control.sendGamepadState(this._index, false);
      this._physicalGamepadId = -1;
      if (this._rumbleInterval !== void 0) {
        clearInterval(this._rumbleInterval);
      }
      if (this._options.enable_keyboard === true) {
        window.removeEventListener("keydown", this._listener.keyDown);
        window.removeEventListener("keyup", this._listener.keyUp);
      }
      if (this._options.enable_gamepad === true) {
        window.removeEventListener("gamepadconnected", this._listener.gamepadConnected);
        window.removeEventListener("gamepaddisconnected", this._listener.gamepadDisconnected);
      }
      if (this._options.enable_gamepad === true && this._options.gamepad_force_capture === false) {
        window.removeEventListener("blur", this._listener.windowBlur);
        window.removeEventListener("focus", this._listener.windowFocus);
      }
    }
    sendButtonState(button, value) {
      if (this._player === void 0) {
        console.log("[Gamepad] Player is not attached. this._player is:", this._player);
        return;
      }
      if (!(button in this.getDefaultFamepadFrame())) {
        console.log("[Gamepad] Invalid button:", button);
        return;
      }
      const buttonPreset = this.getDefaultFamepadFrame();
      buttonPreset.GamepadIndex = this._index;
      this._gamepadOverride.buttons[button] = value;
    }
    onKeyDown(event) {
      if (this._isFocused === false) {
        return;
      }
      for (const button in this._options.keyboard_mapping) {
        if (this._options.keyboard_mapping[button] === event.key) {
          this._gamepadOverride.buttons[button] = 1;
          event.preventDefault();
        }
      }
    }
    onKeyUp(event) {
      if (this._isFocused === false) {
        return;
      }
      for (const button in this._options.keyboard_mapping) {
        if (this._options.keyboard_mapping[button] === event.key) {
          this._gamepadOverride.buttons[button] = 0;
          event.preventDefault();
        }
      }
    }
    onWindowFocus() {
      this._isFocused = true;
    }
    onWindowBlur() {
    }
    getDefaultFamepadFrame() {
      return {
        GamepadIndex: 0,
        Nexus: 0,
        Menu: 0,
        View: 0,
        A: 0,
        B: 0,
        X: 0,
        Y: 0,
        DPadUp: 0,
        DPadDown: 0,
        DPadLeft: 0,
        DPadRight: 0,
        LeftShoulder: 0,
        RightShoulder: 0,
        LeftThumb: 0,
        RightThumb: 0,
        LeftThumbXAxis: 0,
        LeftThumbYAxis: 0,
        RightThumbXAxis: 0,
        RightThumbYAxis: 0,
        LeftTrigger: 0,
        RightTrigger: 0
      };
    }
    detectActiveGamepad() {
      if (this._physicalGamepadId < 0) {
        for (const gamepadBrowser in navigator.getGamepads()) {
          const gamepadIndex = navigator.getGamepads()[gamepadBrowser]?.index;
          if (gamepadIndex === void 0) {
            continue;
          }
          let gamepadFoundInHandler = false;
          for (const gamepad in this._player?._channels.control.getGamepadHandlers()) {
            if (this._player?._channels.control.getGamepadHandlers()[gamepad] instanceof _Gamepad && (this._player?._channels.control.getGamepadHandlers()[gamepad])._physicalGamepadId === navigator.getGamepads()[gamepadBrowser]?.index) {
              console.log("[Gamepad] Active gamepad already detected:", gamepadIndex, (this._player?._channels.control.getGamepadHandlers()[gamepad])._physicalGamepadId);
              gamepadFoundInHandler = true;
              continue;
            }
          }
          if (gamepadFoundInHandler === false) {
            this._physicalGamepadId = gamepadIndex;
            console.log("[Gamepad] Active gamepad detected:", this._physicalGamepadId);
          }
        }
      } else {
        console.log("[Gamepad] Physical gamepad already detected:", this._physicalGamepadId);
      }
    }
    onGamepadConnected(event) {
      if (this._physicalGamepadId < 0) {
        for (const gamepad in this._player?._channels.control.getGamepadHandlers()) {
          if (this._player?._channels.control.getGamepadHandlers()[gamepad] instanceof _Gamepad && (this._player?._channels.control.getGamepadHandlers()[gamepad])._physicalGamepadId === event.gamepad.index) {
            return;
          }
        }
        this._physicalGamepadId = event.gamepad.index;
      }
    }
    onGamepadDisconnected(event) {
      if (event.gamepad.index === this._physicalGamepadId) {
        this._physicalGamepadId = -1;
      }
    }
    hasOverrideButtons() {
      return Object.keys(this._gamepadOverride.buttons).length > 0;
    }
    getGamepadState() {
      const gamepad = this.getGamepad(this._physicalGamepadId);
      if ((this._physicalGamepadId < 0 || gamepad === void 0 || this._isFocused === false) && this.hasOverrideButtons() === false) {
        return void 0;
      }
      const frame = this.getDefaultFamepadFrame();
      frame.GamepadIndex = this._index;
      if (gamepad !== void 0) {
        for (const button in this._options.gamepad_mapping) {
          frame[button] = gamepad.buttons[this._options.gamepad_mapping[button]].value;
        }
        for (const axis in this._options.gamepad_axes_mapping) {
          frame[axis] = this.normaliseAxis(gamepad.axes[this._options.gamepad_axes_mapping[axis]]);
        }
      }
      for (const button in this._gamepadOverride.buttons) {
        frame[button] = this._gamepadOverride.buttons[button];
      }
      const buttonsToRemove = [];
      for (const button in this._gamepadOverride.buttons) {
        if (this._gamepadOverride.buttons[button] === 0) {
          buttonsToRemove.push(button);
        }
      }
      for (const button of buttonsToRemove) {
        delete this._gamepadOverride.buttons[button];
      }
      if (frame.View > 0 && frame.Menu > 0) {
        frame.View = 0;
        frame.Menu = 0;
        frame.Nexus = 1;
      }
      return frame;
    }
    getGamepad(index = -1) {
      const gamepad = navigator.getGamepads()[index];
      if (gamepad !== null) {
        return gamepad;
      } else {
        return void 0;
      }
    }
    getPhysicalGamepadId() {
      return this._physicalGamepadId;
    }
    handleVibration(report) {
      if (this._physicalGamepadId < 0) {
        console.log("[Gamepad] Received a vibration report but no physical gamepad is connected");
        return;
      }
      const gamepad = this.getGamepad(this._physicalGamepadId);
      if (gamepad === void 0) {
        console.log("[Gamepad] Received a vibration report but no physical gamepad is connected");
        return;
      }
      if (this._options.enable_vibration === false) {
        return;
      }
      const rumbleData = {
        startDelay: 0,
        duration: report.durationMs,
        weakMagnitude: report.rightMotorPercent,
        strongMagnitude: report.leftMotorPercent,
        leftTrigger: report.leftTriggerMotorPercent,
        rightTrigger: report.rightTriggerMotorPercent
      };
      if (gamepad.vibrationActuator?.type === "dual-rumble") {
        const intensityRumble = report.rightMotorPercent < 0.6 ? (0.6 - report.rightMotorPercent) / 2 : 0;
        const intensityRumbleTriggers = (report.leftTriggerMotorPercent + report.rightTriggerMotorPercent) / 4;
        const endIntensity = Math.min(intensityRumble, intensityRumbleTriggers);
        rumbleData.weakMagnitude = Math.min(1, report.rightMotorPercent + endIntensity);
        rumbleData.leftTrigger = 0;
        rumbleData.rightTrigger = 0;
        gamepad.vibrationActuator?.playEffect("dual-rumble", rumbleData);
        clearInterval(this._rumbleInterval);
        if (report.repeat > 0) {
          let repeatCount = report.repeat;
          this._rumbleInterval = setInterval(() => {
            if (repeatCount <= 0) {
              clearInterval(this._rumbleInterval);
            }
            if (gamepad.vibrationActuator !== void 0) {
              gamepad.vibrationActuator?.playEffect("dual-rumble", rumbleData);
            }
            repeatCount--;
          }, report.delayMs + report.durationMs);
        }
      } else {
        console.log("[Gamepad] Unknown vibration type:", gamepad.vibrationActuator.type);
      }
    }
    normaliseAxis(value) {
      if (Math.abs(value) < this._options.gamepad_deadzone) {
        return 0;
      }
      value = value - Math.sign(value) * this._options.gamepad_deadzone;
      value /= 1 - this._options.gamepad_deadzone;
      return value;
    }
  };

  // xcloud-player/src/lib/channel/control.ts
  var ControlChannel = class extends Channel {
    constructor() {
      super(...arguments);
      this._gamepadHandlers = {
        0: void 0,
        1: void 0,
        2: void 0,
        3: void 0
      };
    }
    getChannelName() {
      return "control";
    }
    getChannelConfig() {
      return {
        protocol: "controlV1",
        ordered: true
      };
    }
    destroy() {
      clearInterval(this._keyframeInterval);
      this._gamepadHandlers[0]?.detach();
      this._gamepadHandlers[1]?.detach();
      this._gamepadHandlers[2]?.detach();
      this._gamepadHandlers[3]?.detach();
    }
    sendAuthorization() {
      const authRequest = JSON.stringify({
        "message": "authorizationRequest",
        "accessKey": "4BDB3609-C1F1-4195-9B37-FEFF45DA8B8E"
      });
      this.send(authRequest);
      this.sendGamepadState(0, true);
      this.sendGamepadState(0, false);
    }
    sendGamepadState(gamepadIndex, wasAdded = true, handler = void 0) {
      if (wasAdded === true) {
        if (handler !== void 0) {
          if (this._gamepadHandlers[gamepadIndex] !== void 0) {
            this._gamepadHandlers[gamepadIndex]?.detach();
          }
          this._gamepadHandlers[gamepadIndex] = handler;
        }
      } else {
        this._gamepadHandlers[gamepadIndex] = void 0;
      }
      const gamepadRequest = JSON.stringify({
        "message": "gamepadChanged",
        "gamepadIndex": gamepadIndex,
        "wasAdded": wasAdded
      });
      this.send(gamepadRequest);
    }
    requestKeyframeRequest(ifrRequested = false) {
      const keyframeRequest = JSON.stringify({
        message: "videoKeyframeRequested",
        ifrRequested
      });
      this.send(keyframeRequest);
    }
    getGamepadHandlers() {
      return this._gamepadHandlers;
    }
    getGamepadHandler(index) {
      for (const gamepad in this.getPlayer()._channels.control.getGamepadHandlers()) {
        if (this.getPlayer()._channels.control.getGamepadHandlers()[gamepad] instanceof Gamepad && this.getPlayer()._channels.control.getGamepadHandlers()[gamepad].getPhysicalGamepadId() === index) {
          return this.getPlayer()._channels.control.getGamepadHandlers()[gamepad];
        }
      }
      return void 0;
    }
  };

  // xcloud-player/src/lib/channel/input/packet.ts
  var InputPacket = class {
    constructor(sequence) {
      this._reportType = 0 /* None */;
      this._totalSize = -1;
      this._sequence = -1;
      this._metadataFrames = [];
      this._gamepadFrames = [];
      this._pointerFrames = [];
      this._mouseFrames = [];
      this._keyboardFrames = [];
      this._maxTouchpoints = 0;
      this._serverVideoWidth = 0;
      this._serverVideoHeight = 0;
      this._sequence = sequence;
    }
    setStreamConfig(options) {
      this._serverVideoWidth = options.width;
      this._serverVideoHeight = options.height;
    }
    setMetadata(maxTouchpoints = 1) {
      this._reportType = 8 /* ClientMetadata */;
      this._totalSize = 15;
      this._maxTouchpoints = maxTouchpoints;
    }
    setData(metadataQueue, gamepadQueue, pointerQueue, mouseQueue, keyboardQueue) {
      let size = 14;
      if (metadataQueue.length > 0) {
        this._reportType |= 1 /* Metadata */;
        size = size + this._calculateMetadataSize(metadataQueue);
        this._metadataFrames = metadataQueue;
      }
      if (gamepadQueue.length > 0) {
        this._reportType |= 2 /* Gamepad */;
        size = size + this._calculateGamepadSize(gamepadQueue);
        this._gamepadFrames = gamepadQueue;
      }
      if (pointerQueue.length > 0) {
        this._reportType |= 4 /* Pointer */;
        size = size + this._calculatePointerSize(pointerQueue);
        this._pointerFrames = pointerQueue;
      }
      if (mouseQueue.length > 0) {
        this._reportType |= 32 /* Mouse */;
        size = size + this._calculateMouseSize(mouseQueue);
        this._mouseFrames = mouseQueue;
      }
      if (keyboardQueue.length > 0) {
        this._reportType |= 64 /* Keyboard */;
        size = size + this._calculateKeyboardSize(keyboardQueue);
        this._keyboardFrames = keyboardQueue;
      }
      this._totalSize = size;
    }
    _calculateMetadataSize(frames) {
      return 1 + 7 * 4 * frames.length;
    }
    _calculateGamepadSize(frames) {
      return 1 + 23 * frames.length;
    }
    _calculatePointerSize(frames) {
      let pointerSize = 1;
      for (const frame in frames) {
        pointerSize = pointerSize + 1 + frames[frame].events.length * 20;
      }
      return pointerSize;
    }
    _calculateMouseSize(frames) {
      return 1 + 18 * frames.length;
    }
    _calculateKeyboardSize(frames) {
      return 1 + 3 * frames.length;
    }
    _writeMetadataData(packet, offset, frames) {
      packet.setUint8(offset, frames.length);
      offset++;
      if (frames.length >= 30) {
        console.warn("metadataQueue is bigger then 30. This might impact reliability!");
      }
      for (let i = 0; i < frames.length; i++) {
        const frame = frames[i];
        const firstFramePacketArrivalTimeMs = frame.firstFramePacketArrivalTimeMs;
        const frameSubmittedTimeMs = frame.frameSubmittedTimeMs;
        const frameDecodedTimeMs = frame.frameDecodedTimeMs;
        const frameRenderedTimeMs = frame.frameRenderedTimeMs;
        const framePacketTime = performance.now();
        const frameDateNow = performance.now();
        packet.setUint32(offset, frame.serverDataKey, true);
        packet.setUint32(offset + 4, firstFramePacketArrivalTimeMs, true);
        packet.setUint32(offset + 8, frameSubmittedTimeMs, true);
        packet.setUint32(offset + 12, frameDecodedTimeMs, true);
        packet.setUint32(offset + 16, frameRenderedTimeMs, true);
        packet.setUint32(offset + 20, framePacketTime, true);
        packet.setUint32(offset + 24, frameDateNow, true);
        offset += 28;
      }
      return offset;
    }
    _writeGamepadData(packet, offset, frames) {
      packet.setUint8(offset, frames.length);
      offset++;
      if (frames.length >= 30) {
        console.warn("gamepadQueue is bigger then 30. This might impact reliability!");
      }
      for (let i = 0; i < frames.length; i++) {
        const input = frames[i];
        packet.setUint8(offset, input.GamepadIndex);
        offset++;
        let buttonMask = 0;
        if (input.Nexus > 0) {
          buttonMask |= 2;
        }
        if (input.Menu > 0) {
          buttonMask |= 4;
        }
        if (input.View > 0) {
          buttonMask |= 8;
        }
        if (input.A > 0) {
          buttonMask |= 16;
        }
        if (input.B > 0) {
          buttonMask |= 32;
        }
        if (input.X > 0) {
          buttonMask |= 64;
        }
        if (input.Y > 0) {
          buttonMask |= 128;
        }
        if (input.DPadUp > 0) {
          buttonMask |= 256;
        }
        if (input.DPadDown > 0) {
          buttonMask |= 512;
        }
        if (input.DPadLeft > 0) {
          buttonMask |= 1024;
        }
        if (input.DPadRight > 0) {
          buttonMask |= 2048;
        }
        if (input.LeftShoulder > 0) {
          buttonMask |= 4096;
        }
        if (input.RightShoulder > 0) {
          buttonMask |= 8192;
        }
        if (input.LeftThumb > 0) {
          buttonMask |= 16384;
        }
        if (input.RightThumb > 0) {
          buttonMask |= 32768;
        }
        packet.setUint16(offset, buttonMask, true);
        packet.setInt16(offset + 2, this._normalizeAxisValue(input.LeftThumbXAxis), true);
        packet.setInt16(offset + 4, this._normalizeAxisValue(-input.LeftThumbYAxis), true);
        packet.setInt16(offset + 6, this._normalizeAxisValue(input.RightThumbXAxis), true);
        packet.setInt16(offset + 8, this._normalizeAxisValue(-input.RightThumbYAxis), true);
        packet.setUint16(offset + 10, this._normalizeTriggerValue(input.LeftTrigger), true);
        packet.setUint16(offset + 12, this._normalizeTriggerValue(input.RightTrigger), true);
        packet.setUint32(offset + 14, 1, true);
        packet.setUint32(offset + 18, 1, false);
        offset += 22;
      }
      return offset;
    }
    _writePointerData(packet, offset, frames) {
      packet.setUint8(offset, 1);
      offset++;
      if (frames.length >= 2) {
        console.warn("pointerQueue is bigger then 1. Only one event will be sent.");
      }
      const frame = frames[0];
      if (frame !== void 0) {
        packet.setUint8(offset, frame.events.length);
        offset++;
        const screenWidth = this._serverVideoWidth;
        const screenHeight = this._serverVideoHeight;
        for (let i = 0; i < frame.events.length; i++) {
          const event = frame.events[i];
          const rect = event.target.getBoundingClientRect();
          let e = 0.06575749909301447 * (screenHeight / 1);
          let n = 0.06575749909301447 * (screenWidth / 1);
          e = 1;
          n = 1;
          if (event.type === "pointerup") {
            e = 0;
            n = 0;
          }
          packet.setUint16(offset, e, true);
          packet.setUint16(offset + 2, n, true);
          packet.setUint8(offset + 4, 255 * event.pressure);
          packet.setUint16(offset + 5, event.twist, true);
          packet.setUint32(offset + 7, 0, true);
          let o = (event.x - rect.left) * (screenWidth / rect.width), l = (event.y - rect.top) * (screenHeight / rect.height);
          if (event.type === "pointerup") {
            o = 0;
            l = 0;
          }
          packet.setUint32(offset + 11, o, true);
          packet.setUint32(offset + 15, l, true);
          packet.setUint8(offset + 19, event.type === "pointerdown" ? 1 : event.type === "pointerup" ? 2 : event.type === "pointermove" ? 3 : 0);
          offset = offset + 20;
        }
      }
      return offset;
    }
    _writeMouseData(packet, offset, frames) {
      packet.setUint8(offset, frames.length);
      offset++;
      if (frames.length >= 30) {
        console.warn("mouseQueue is bigger then 30. This might impact reliability!");
      }
      for (let i = 0; i < frames.length; i++) {
        const input = frames[i];
        packet.setUint32(offset, input.X, true);
        packet.setUint32(offset + 4, input.Y, true);
        packet.setUint32(offset + 8, input.WheelX, true);
        packet.setUint32(offset + 12, input.WheelY, true);
        packet.setUint8(offset + 16, input.Buttons);
        packet.setUint8(offset + 17, input.Relative);
        offset += 18;
      }
      return offset;
    }
    _writeKeyboardData(packet, offset, frames) {
      packet.setUint8(offset, frames.length);
      offset++;
      if (frames.length >= 30) {
        console.warn("keyboardQueue is bigger then 30. This might impact reliability!");
      }
      for (let i = 0; i < frames.length; i++) {
        const input = frames[i];
        packet.setUint8(offset, 2);
        packet.setUint8(offset + 1, input.pressed ? 1 : 0);
        packet.setUint8(offset + 2, input.keyCode);
        offset += 3;
      }
      return offset;
    }
    toBuffer() {
      const metadataAlloc = new Uint8Array(this._totalSize);
      const packet = new DataView(metadataAlloc.buffer);
      packet.setUint16(0, this._reportType, true);
      packet.setUint32(2, this._sequence, true);
      packet.setFloat64(6, performance.now(), true);
      let offset = 14;
      if (this._metadataFrames.length > 0) {
        offset = this._writeMetadataData(packet, offset, this._metadataFrames);
      }
      if (this._gamepadFrames.length > 0) {
        offset = this._writeGamepadData(packet, offset, this._gamepadFrames);
      }
      if (this._pointerFrames.length > 0) {
        offset = this._writePointerData(packet, offset, this._pointerFrames);
      }
      if (this._mouseFrames.length > 0) {
        offset = this._writeMouseData(packet, offset, this._mouseFrames);
      }
      if (this._keyboardFrames.length > 0) {
        offset = this._writeKeyboardData(packet, offset, this._keyboardFrames);
      }
      if (this._reportType === 8 /* ClientMetadata */) {
        packet.setUint8(offset, this._maxTouchpoints);
        offset++;
      }
      if (offset !== offset) {
        throw new Error("Packet length mismatch. Something is wrong!");
      }
      return packet;
    }
    _normalizeTriggerValue(value) {
      if (value <= 0 || Number.isNaN(value)) {
        return 0;
      }
      if (value >= 1) {
        return 65535;
      }
      return value * 65535 | 0;
    }
    _normalizeAxisValue(value) {
      const max = 32767;
      const min = -32767;
      const scaled = Math.round(value * max);
      return scaled > max ? max : scaled < min ? min : scaled;
    }
  };

  // xcloud-player/src/lib/channel/input/queue.ts
  var InputQueue = class {
    constructor(player) {
      this._inputSequenceNum = 0;
      this._metadataQueue = [];
      this._gamepadQueue = [];
      this._pointerQueue = [];
      this._mouseQueue = [];
      this._keyboardQueue = [];
      // Axis change detection and heartbeat tracking
      this._gamepadHeartbeatTimers = /* @__PURE__ */ new Map();
      this._lastGamepadState = /* @__PURE__ */ new Map();
      this._axisChangeThreshold = 0.08;
      this._player = player;
    }
    _hasQueuedData() {
      return this._metadataQueue.length > 0 || this._gamepadQueue.length > 0 || this._pointerQueue.length > 0 || this._mouseQueue.length > 0 || this._keyboardQueue.length > 0;
    }
    _upsertGamepadFrame(data) {
      for (let i = 0; i < this._gamepadQueue.length; i++) {
        if (this._gamepadQueue[i].GamepadIndex === data.GamepadIndex) {
          this._gamepadQueue[i] = data;
          return;
        }
      }
      this._gamepadQueue.push(data);
    }
    _hasSignificantAxisChange(current, last) {
      if (!last) return true;
      const stickNames = ["LeftThumbXAxis", "LeftThumbYAxis", "RightThumbXAxis", "RightThumbYAxis"];
      const triggerNames = ["LeftTrigger", "RightTrigger"];
      for (const stick of stickNames) {
        const delta = Math.abs(current[stick] - last[stick]);
        const value = Math.abs(current[stick]);
        if (delta > this._axisChangeThreshold || value > 0.15) {
          return true;
        }
      }
      for (const trigger of triggerNames) {
        if (Math.abs(current[trigger] - last[trigger]) > 0.01) {
          return true;
        }
      }
      return false;
    }
    _isHeartbeatDue(gamepadIndex) {
      const lastHeartbeat = this._gamepadHeartbeatTimers.get(gamepadIndex) ?? 0;
      return performance.now() - lastHeartbeat > 33;
    }
    queueMetadataFrame(data) {
      this._metadataQueue.push(data);
      this.checkQueueAndSend();
    }
    queueGamepadFrame(data) {
      const lastFrame = this._lastGamepadState.get(data.GamepadIndex);
      const hasChange = this._hasSignificantAxisChange(data, lastFrame);
      const isDue = this._isHeartbeatDue(data.GamepadIndex);
      if (hasChange || isDue) {
        this._upsertGamepadFrame(data);
        this._lastGamepadState.set(data.GamepadIndex, data);
        this._gamepadHeartbeatTimers.set(data.GamepadIndex, performance.now());
        this.checkQueueAndSend();
      }
    }
    queueGamepadFrames(frames, forceSend = false) {
      for (const frame of frames) {
        this._upsertGamepadFrame(frame);
      }
      this.checkQueueAndSend(forceSend);
    }
    queueMouseFrame(data) {
      this._mouseQueue.push(data);
      this.checkQueueAndSend();
    }
    queueKeyboardFrame(data) {
      this._keyboardQueue.push(data);
      this.checkQueueAndSend();
    }
    queuePointerFrame(data) {
      if (this._pointerQueue.length > 0) {
        this._pointerQueue[0].events = this._pointerQueue[0].events.concat(data.events);
      } else {
        this._pointerQueue.push(data);
      }
      this.checkQueueAndSend();
    }
    checkQueueAndSend(forceSend = false) {
      if (forceSend === true) {
        if (this._hasQueuedData()) {
          this.sendQueue();
        }
        return;
      }
      if (this._gamepadQueue.length > 0) {
        this.sendQueue();
        return;
      }
      if (this._mouseQueue.length > 0 || this._keyboardQueue.length > 0 || this._pointerQueue.length > 0) {
        this.sendQueue();
        return;
      }
      if (this._metadataQueue.length > 5) {
        this.sendQueue();
      }
    }
    sendQueue() {
      const packet = new InputPacket(this.getSequenceNum());
      if (this._pointerQueue.length > 0) {
        packet.setStreamConfig({
          width: this._player._channels.input.getServerVideoWidth(),
          height: this._player._channels.input.getServerVideoHeight()
        });
      }
      packet.setData(this._metadataQueue, this._gamepadQueue, this._pointerQueue, this._mouseQueue, this._keyboardQueue);
      this._player._channels.input.send(packet.toBuffer());
      this.clearQueues();
    }
    clearQueues() {
      this._metadataQueue = [];
      this._gamepadQueue = [];
      this._pointerQueue = [];
      this._mouseQueue = [];
      this._keyboardQueue = [];
    }
    getSequenceNum() {
      this._inputSequenceNum++;
      return this._inputSequenceNum;
    }
  };

  // xcloud-player/src/lib/channel/input.ts
  var InputChannel = class extends Channel {
    constructor() {
      super(...arguments);
      this._serverVideoWidth = 0;
      this._serverVideoHeight = 0;
      this._inputQueue = new InputQueue(this.getPlayer());
    }
    getChannelName() {
      return "input";
    }
    getChannelConfig() {
      return {
        ordered: true,
        protocol: "1.0"
      };
    }
    getServerVideoWidth() {
      return this._serverVideoWidth;
    }
    getServerVideoHeight() {
      return this._serverVideoHeight;
    }
    start() {
      const Packet = new InputPacket(0);
      Packet.setMetadata(navigator.maxTouchPoints > 1 ? navigator.maxTouchPoints : 1);
      this.send(Packet.toBuffer());
      console.log("[InputChannel] Sent metadata:", Packet, Packet.toBuffer());
      setTimeout(this.gamepadStateLoop.bind(this), 16);
    }
    onMessage(event) {
      const dataView = new DataView(event.data);
      const reportType = dataView.getUint8(0);
      console.log("[InputChannel] received:", dataView);
      if (reportType === 128 /* Vibration */) {
        console.log("Received a vibration report");
        dataView.getUint8(2);
        const gamepadIndex = dataView.getUint8(3);
        const gamepad = this.getPlayer()._channels.control.getGamepadHandler(gamepadIndex);
        if (gamepad !== void 0) {
          const leftMotorPercent = dataView.getUint8(4) / 100;
          const rightMotorPercent = dataView.getUint8(5) / 100;
          const leftTriggerMotorPercent = dataView.getUint8(6) / 100;
          const rightTriggerMotorPercent = dataView.getUint8(7) / 100;
          const durationMs = dataView.getUint16(8, true);
          const delayMs = dataView.getUint16(10, true);
          const repeat = dataView.getUint8(12);
          const rumble = {
            gamepadIndex,
            leftMotorPercent,
            rightMotorPercent,
            leftTriggerMotorPercent,
            rightTriggerMotorPercent,
            durationMs,
            delayMs,
            repeat
          };
          gamepad.handleVibration(rumble);
        } else {
          console.log("[InputChannel] Received a vibration report but no gamepad handler is available");
        }
      } else if (reportType === 16 /* ServerMetadata */) {
        console.log("[InputChannel] Received server video dimensions:", dataView.getUint32(2, true), dataView.getUint32(6, true));
        this._serverVideoHeight = dataView.getUint32(2, true);
        this._serverVideoWidth = dataView.getUint32(6, true);
      }
    }
    gamepadStateLoop() {
      const gamepadHandlers = this.getPlayer()._channels.control.getGamepadHandlers();
      const gamepadFrames = [];
      for (const gamepad in gamepadHandlers) {
        if (gamepadHandlers[gamepad] instanceof Gamepad) {
          const frame = gamepadHandlers[gamepad].getGamepadState();
          if (frame !== void 0) {
            gamepadFrames.push(frame);
          }
        }
      }
      if (gamepadFrames.length > 0) {
        this.queueGamepadFrames(gamepadFrames);
      }
      setTimeout(this.gamepadStateLoop.bind(this), 16);
    }
    queueMetadataFrame(data) {
      return this._inputQueue.queueMetadataFrame(data);
    }
    queueGamepadFrames(frames, forceSend = false) {
      this._inputQueue.queueGamepadFrames(frames, forceSend);
    }
    queueMouseFrame(data) {
      this._inputQueue.queueMouseFrame(data);
    }
    queueKeyboardFrame(data) {
      this._inputQueue.queueKeyboardFrame(data);
    }
    queuePointerFrame(data) {
      this._inputQueue.queuePointerFrame(data);
    }
    destroy() {
    }
  };

  // xcloud-player/src/lib/channel/message/handler.ts
  var MessageHandler = class {
    constructor(channel, data) {
      this._channel = channel;
      this._data = data;
      this.handle(this._data);
    }
    handle(data) {
      console.log("[MessageHandler] Handling:", data);
      switch (data.target) {
        case "/streaming/sessionLifetimeManagement/serverInitiatedDisconnect":
          this.completeTransaction(data.id, "");
          this._channel.getPlayer().destroy();
          break;
        case "/streaming/properties/titleinfo":
          console.log("Title update:", data);
          break;
        case "/streaming/touchcontrols/showlayoutv2":
          break;
        case "/streaming/systemUi/messages/ShowMessageDialog":
          this.handleMessage(data);
          break;
        default:
          console.log("[MessageChannel] Unhandled transaction:", data);
      }
    }
    completeTransaction(id, data) {
      const transaction = JSON.stringify({
        "type": "TransactionComplete",
        "content": JSON.stringify(data),
        // 'content':'{\'Result\':0}',
        "id": id,
        "cv": ""
      });
      this._channel.send(transaction);
    }
    cancelTransaction(id, data) {
      const transaction = JSON.stringify({
        "type": "ReceiverCancel",
        "content": JSON.stringify(data),
        "id": id,
        "cv": ""
      });
      this._channel.send(transaction);
    }
    handleMessage(data) {
      const jsonData = JSON.parse(data.content);
      console.log(jsonData);
      if (confirm(jsonData.TitleText + "\n\n" + jsonData.ContentText)) {
        this.completeTransaction(data.id, { Result: 0 });
      } else {
        this.completeTransaction(data.id, { Result: 1 });
      }
    }
  };

  // xcloud-player/src/lib/channel/message.ts
  var uuidv4 = () => crypto.randomUUID();
  var MessageChannel = class extends Channel {
    constructor() {
      super(...arguments);
      this._handshakeCompleted = false;
    }
    getChannelName() {
      return "message";
    }
    getChannelConfig() {
      return {
        protocol: "messageV1",
        ordered: true
      };
    }
    onOpen() {
      console.log("[MessageChannel] Sending handshake...");
      const handshake = JSON.stringify({
        "type": "Handshake",
        "version": "messageV1",
        "id": "be0bfc6d-1e83-4c8a-90ed-fa8601c5a179",
        "cv": "0"
      });
      this.send(handshake);
    }
    onMessage(event) {
      const data = JSON.parse(event.data);
      switch (data.type) {
        case "HandshakeAck":
          console.log("[MessageChannel] Handshake completed");
          this._handshakeCompleted = true;
          this.getPlayer()._channels.control.sendAuthorization();
          this.getPlayer()._channels.input.start();
          this.sendConfig();
          break;
        case "TransactionStart":
        case "Message":
          new MessageHandler(this, data);
          break;
        default:
          console.log("[MessageChannel] Unhandled message type:", data.type, data);
      }
    }
    sendConfig() {
      const systemUis = [];
      const systemVersion = [0, 2, 0];
      const uiConfig = JSON.stringify(this.generateMessage("/streaming/systemUi/configuration", {
        "version": systemVersion,
        "systemUis": systemUis
        // Xbox Windows app has [33], xCloud has [10,19,31,27,32,-41]
        // 8 = unknown
        // 10 = ShowVirtualKeyboard
        // 13 = unknown
        // 15 = unknown
        // 19 = ShowMessageDialog
        // 31 = ShowApplication
        // 27 = ShowPurchase
        // 32 = ShowTimerExtensions
        // 33 = Xbox windows app, disables the nexus menu on xCloud (Alt nexus menu?)
        // -44 = unknown
        // 40 = unknown
        // 41 = unknown
        // -43 = unknown
        // Possible options: Keyboard, PurchaseModal
      }));
      this.send(uiConfig);
      const clientConfig = JSON.stringify(this.generateMessage("/streaming/properties/clientappinstallidchanged", { "clientAppInstallId": "c97d7ee0-73b2-4239-bf1d-9d805a338429" }));
      this.send(clientConfig);
      const orientationConfig = JSON.stringify(this.generateMessage("/streaming/characteristics/orientationchanged", { "orientation": 0 }));
      this.send(orientationConfig);
      const touchConfig = JSON.stringify(this.generateMessage("/streaming/characteristics/touchinputenabledchanged", { "touchInputEnabled": false }));
      this.send(touchConfig);
      const deviceConfig = JSON.stringify(this.generateMessage("/streaming/characteristics/clientdevicecapabilities", {}));
      this.send(deviceConfig);
      const dimensionsConfig = JSON.stringify(this.generateMessage("/streaming/characteristics/dimensionschanged", {
        "horizontal": 1920,
        "vertical": 1080,
        "preferredWidth": 1920,
        "preferredHeight": 1080,
        "safeAreaLeft": 0,
        "safeAreaTop": 0,
        "safeAreaRight": 1920,
        "safeAreaBottom": 1080,
        "supportsCustomResolution": true
      }));
      this.send(dimensionsConfig);
    }
    generateMessage(path, data) {
      return {
        "type": "Message",
        "content": JSON.stringify(data),
        "id": uuidv4(),
        "target": path,
        "cv": ""
      };
    }
    destroy() {
    }
  };

  // xcloud-player/src/lib/teredo.ts
  var Teredo = class {
    constructor(address) {
      this._address = address;
      const split_str = this._address.split(":");
      const ipv4_part = split_str[6] + split_str[7];
      const ipv4_port_part = split_str[5];
      const ip_1 = ~parseInt(ipv4_part.substring(0, 2), 16) & 255;
      const ip_2 = ~parseInt(ipv4_part.substring(2, 4), 16) & 255;
      const ip_3 = ~parseInt(ipv4_part.substring(4, 6), 16) & 255;
      const ip_4 = ~parseInt(ipv4_part.substring(6, 8), 16) & 255;
      const port_1 = ~parseInt(ipv4_port_part.substring(0, 4), 16) & 65535;
      this._ipv4Address = ip_1 + "." + ip_2 + "." + ip_3 + "." + ip_4;
      this._ipv4Port = port_1;
    }
    getIpv4Address() {
      return this._ipv4Address;
    }
    getIpv4Port() {
      return this._ipv4Port;
    }
  };

  // xcloud-player/src/lib/ice.ts
  var Ice = class {
    constructor(player) {
      this._iceCandidates = [];
      this._peerConnection = player._peerConnection;
      this._peerConnection.addEventListener("icecandidate", (event) => {
        if (event.candidate) {
          this._iceCandidates.push(event.candidate);
        }
      });
    }
    getCandidates() {
      return this._iceCandidates;
    }
    setRemoteCandidates(candidates) {
      if (candidates.length === 0) {
        console.warn("Error: No candidates received!");
      }
      for (const candidate in candidates) {
        if (candidates[candidate].candidate === "a=end-of-candidates") {
          continue;
        }
        const candidateAddress = candidates[candidate].candidate.split(" ");
        if (candidateAddress.length > 4 && candidateAddress[4].substr(0, 4) === "2001") {
          const teredo = new Teredo(candidateAddress[4]);
          this._peerConnection.addIceCandidate({
            candidate: "a=candidate:10 1 UDP 1 " + teredo.getIpv4Address() + " 9002 typ host ",
            sdpMLineIndex: candidates[candidate].sdpMLineIndex,
            sdpMid: candidates[candidate].sdpMid
          });
          this._peerConnection.addIceCandidate({
            candidate: "a=candidate:11 1 UDP 1 " + teredo.getIpv4Address() + " " + teredo.getIpv4Port() + " typ host ",
            sdpMLineIndex: candidates[candidate].sdpMLineIndex,
            sdpMid: candidates[candidate].sdpMid
          });
        }
        this._peerConnection.addIceCandidate({
          candidate: candidates[candidate].candidate,
          sdpMid: candidates[candidate].sdpMid,
          sdpMLineIndex: candidates[candidate].sdpMLineIndex
        });
      }
    }
  };

  // xcloud-player/src/lib/sdp.ts
  var Sdp = class {
    constructor(player) {
      this._player = player;
      this._peerConnection = player._peerConnection;
    }
    getAvailableCodecs() {
      const capabilities = RTCRtpReceiver.getCapabilities("video")?.codecs;
      if (capabilities === void 0) {
        return [];
      }
      const codecs = [];
      for (const codec in capabilities) {
        if (!codecs.includes(capabilities[codec].mimeType)) {
          codecs.push(capabilities[codec].mimeType);
        }
      }
      return codecs;
    }
    // getServerCodecs() {
    //     for(const sender in this._peerConnection.getSenders()){
    //         console.log('Sender:', this._peerConnection.getSenders()[sender])
    //     }
    // }
    setLocalSDP(sdp) {
      console.log("[SDP] setLocalSDP() Setting local SDP:", sdp);
      sdp.sdp = sdp.sdp?.replace("useinbandfec=1", "useinbandfec=1; stereo=1");
      return sdp;
    }
    setRemoteSDP(sdp) {
      console.log("[SDP] setRemoteSDP() Setting remote SDP:", sdp);
      return sdp;
    }
    getDefaultCodecPreferences() {
      const capabilities = RTCRtpReceiver.getCapabilities("video")?.codecs;
      if (capabilities === void 0) {
        return [];
      }
      const t1 = [];
      const t2 = [];
      const t3 = [];
      const t4 = [];
      for (const codec in capabilities) {
        if (capabilities[codec].mimeType.includes("H264")) {
          if (capabilities[codec].sdpFmtpLine?.includes("profile-level-id=4d")) {
            t1.push(capabilities[codec]);
          } else if (capabilities[codec].sdpFmtpLine?.includes("profile-level-id=42e")) {
            t2.push(capabilities[codec]);
          } else if (capabilities[codec].sdpFmtpLine?.includes("profile-level-id=420")) {
            t3.push(capabilities[codec]);
          }
        } else if (capabilities[codec].mimeType.includes("ulpfec")) {
          t4.push(capabilities[codec]);
        } else if (capabilities[codec].mimeType.includes("flexfec")) {
          t4.push(capabilities[codec]);
        } else if (capabilities[codec].mimeType.includes("VP9")) {
          t4.push(capabilities[codec]);
        } else if (capabilities[codec].mimeType.includes("VP8")) {
          t4.push(capabilities[codec]);
        }
      }
      const codecOrder = [...t1, ...t2, ...t3, ...t4];
      return codecOrder;
    }
    _setBitrate(sdp, media, bitrate) {
      const lines = sdp.split("\n");
      let line = -1;
      for (let i = 0; i < lines.length; i++) {
        if (lines[i].indexOf("m=" + media) === 0) {
          line = i;
          break;
        }
      }
      if (line === -1) {
        console.debug("Could not find the m line for", media);
        return sdp;
      }
      line++;
      while (lines[line].indexOf("i=") === 0 || lines[line].indexOf("c=") === 0) {
        line++;
      }
      if (lines[line].indexOf("b") === 0) {
        lines[line] = "b=AS:" + bitrate;
        return lines.join("\n");
      }
      let newLines = lines.slice(0, line);
      newLines.push("b=AS:" + bitrate);
      newLines = newLines.concat(lines.slice(line, lines.length));
      return newLines.join("\n");
    }
  };

  // xcloud-player/src/lib/stats.ts
  var Stats = class {
    constructor(player) {
      this._videoCodec = "";
      this._audioCodec = "";
      this._videoWidth = 0;
      this._videoHeight = 0;
      this._videoFps = 0;
      this._rtt = 0;
      this._remoteHost = "";
      this._remotePort = 0;
      this._remoteIsLocal = false;
      this._remoteIsIpv6 = false;
      this._activeRemoteCandidate = "";
      this._player = player;
      this._peerConnection = player._peerConnection;
      setInterval(this.loop.bind(this), 1e3);
    }
    loop() {
      this._peerConnection.getStats().then((stats) => {
        stats.forEach((report) => {
          if (report.type === "inbound-rtp") {
            this.readInboundRtp(report);
          } else if (report.type === "candidate-pair" && report.packetsReceived !== void 0 && report.packetsReceived > 0) {
            this.readCandidatePair(report);
          } else if (report.type === "remote-candidate") {
            this.readRemoteCandidate(report);
          }
        });
      });
    }
    readInboundRtp(report) {
      if (report.kind === "video" && report.codecId && report.packetsReceived !== void 0 && report.packetsReceived > 0) {
        if (report.codecId.includes("profile-level-id=4d")) {
          this._videoCodec = "H264 (High)";
        } else if (report.codecId.includes("profile-level-id=42e")) {
          this._videoCodec = "H264 (Normal)";
        } else if (report.codecId.includes("profile-level-id=420")) {
          this._videoCodec = "H264 (Low)";
        } else {
          this._videoCodec = "Other (" + report.codecId + ")";
        }
        if (report.frameWidth) {
          this._videoWidth = report.frameWidth;
        }
        if (report.frameHeight) {
          this._videoHeight = report.frameHeight;
        }
        if (report.framesPerSecond) {
          this._videoFps = report.framesPerSecond;
        }
      } else if (report.kind === "audio" && report.codecId && report.packetsReceived !== void 0 && report.packetsReceived > 0) {
        this._audioCodec = report.codecId;
      }
    }
    readCandidatePair(report) {
      this._activeRemoteCandidate = report.remoteCandidateId;
      this._rtt = report.currentRoundTripTime;
    }
    readRemoteCandidate(report) {
      if (report.id !== this._activeRemoteCandidate) {
        return;
      }
      if (this.isPrivateIP(report.address)) {
        this._remoteIsLocal = true;
      } else {
        this._remoteIsLocal = false;
      }
      if (this.isIpv6(report.address)) {
        this._remoteIsIpv6 = true;
      } else {
        this._remoteIsIpv6 = false;
      }
      this._remoteHost = report.address;
      this._remotePort = report.port;
    }
    isPrivateIP(ip) {
      const parts = ip.split(".");
      return parts[0] === "10" || parts[0] === "172" && (parseInt(parts[1], 10) >= 16 && parseInt(parts[1], 10) <= 31) || parts[0] === "192" && parts[1] === "168";
    }
    isIpv6(ip) {
      const parts = ip.split(":");
      return parts.length > 3 ? true : false;
    }
  };

  // xcloud-player/src/lib/render/overlay.ts
  var Overlay = class {
    constructor(videoComponent, player) {
      this._overlays = {
        debug: void 0
      };
      this._videoComponent = videoComponent;
      this._player = player;
    }
    toggleDebug() {
      if (this._overlays.debug === void 0) {
        this.createDebugOverlay();
      } else {
        this.destroy();
      }
    }
    createDebugOverlay() {
      this._overlays.debug = document.createElement("div");
      this._overlays.debug.id = "playerDebugOverlay";
      this._overlays.debug.style.position = "absolute";
      this._overlays.debug.style.top = "0";
      this._overlays.debug.style.left = "0";
      this._overlays.debug.style.width = this._videoComponent.getElement()?.style.width || "100%";
      this._overlays.debug.style.height = this._videoComponent.getElement()?.style.height || "auto";
      this._overlays.debug.style.background = "linear-gradient(0deg, rgba(0,0,0, 0) 0%, rgba(0,0,0, 0) 75%, rgba(0,0,0, 0.5) 100%)";
      this._overlays.debug.style.padding = "10px";
      this._overlays.debug.style.pointerEvents = "none";
      document.getElementById(this._player.getElementId())?.appendChild(this._overlays.debug);
      setTimeout(() => {
        this.refreshDebugOverlay();
      }, 500);
      this.refreshDebugOverlay();
    }
    destroy() {
      this._overlays.debug?.remove();
      this._overlays.debug = void 0;
    }
    refreshDebugOverlay() {
      if (this._overlays.debug !== void 0) {
        this._overlays.debug.innerHTML = "";
        this._overlays.debug.appendChild(this.createLabel("Local Play", this._player.getStats()._remoteIsLocal ? "Local" : "Remote", "ok"));
        this._overlays.debug.appendChild(this.createLabel("Resolution", this._player.getStats()._videoWidth + "x" + this._player.getStats()._videoHeight, "ok"));
        this._overlays.debug.appendChild(this.createLabel("Stream FPS", this._player.getStats()._videoFps.toString(), this._player.getStats()._videoFps >= 58 ? "ok" : "warning"));
        const frameStats = this._videoComponent.getFrameStats?.();
        if (frameStats) {
          this._overlays.debug.appendChild(this.createLabel("Render FPS", frameStats.renderingFps.toString(), frameStats.renderingFps >= 58 ? "ok" : "warning"));
          this._overlays.debug.appendChild(this.createLabel("Render Delay", frameStats.renderDelayMs.toString() + " ms", frameStats.renderDelayMs < 16 ? "ok" : frameStats.renderDelayMs < 33 ? "warning" : "error"));
          this._overlays.debug.appendChild(this.createLabel("Dropped Frames", frameStats.droppedFrames.toString(), frameStats.droppedFrames === 0 ? "ok" : frameStats.droppedFrames < 30 ? "warning" : "error"));
        }
        this._overlays.debug.appendChild(this.createLabel("Connection", this._player.getStats()._remoteIsIpv6 ? "IPv6" : "IPv4", "ok"));
        this._overlays.debug.appendChild(this.createLabel("Video", this._player.getStats()._videoCodec, "ok"));
        const rttButtonState = this._player.getStats()._rtt * 1e3 > 75 ? "error" : this._player.getStats()._rtt * 1e3 > 40 ? "warning" : "ok";
        this._overlays.debug.appendChild(this.createLabel("RTT", (this._player.getStats()._rtt * 1e3).toString() + " ms", rttButtonState));
        setTimeout(() => {
          this.refreshDebugOverlay();
        }, 500);
      }
    }
    createLabel(text, value = "", style = "") {
      const labelStyle = {
        borderRadius: "5px",
        background: "linear-gradient(0deg, rgba(28,26,26,1) 0%, rgba(47,45,45,1) 100%)",
        padding: "10px",
        fontSize: "12px",
        marginTop: "3px",
        marginRight: "10px",
        marginBottom: "2px",
        textTransform: "uppercase",
        color: "white",
        display: "inline-block"
      };
      const element = document.createElement("span");
      const colors = {
        "error": "rgb(220, 53, 69)",
        "warning": "rgb(255, 193, 7)",
        "ok": "rgb(25, 135, 84)"
      };
      if (value !== "") {
        element.innerHTML = `${text}: <span style="background: ${colors[style]}; border-radius: 5px; padding: 4px; padding-left: 8px; padding-right: 8px;">${value}</span>`;
      } else {
        element.innerText = text;
      }
      for (const key in labelStyle) {
        element.style[key] = labelStyle[key];
      }
      return element;
    }
  };

  // xcloud-player/src/lib/render/video.ts
  var VideoComponent = class {
    constructor(player) {
      this._player = player;
      this._overlay = new Overlay(this, this._player);
    }
    create(stream) {
      const videoElement = document.createElement("video");
      videoElement.srcObject = stream;
      videoElement.autoplay = true;
      videoElement.muted = true;
      videoElement.playsInline = true;
      videoElement.style.width = "100%";
      videoElement.style.height = "100%";
      videoElement.style.objectFit = "contain";
      videoElement.style.backgroundColor = "black";
      videoElement.style.touchAction = "none";
      const element = document.getElementById(this._player.getElementId());
      if (element === null) {
        return;
      }
      this._element = videoElement;
      element.appendChild(this._element);
      const isStatic = getComputedStyle(element).position === "static" ? true : false;
      if (isStatic === true) {
        element.style.position = "relative";
      }
      this._element.requestVideoFrameCallback(this.processVideoMetadata.bind(this));
    }
    processVideoMetadata(timestamp, data) {
      if (this._element === void 0) {
        return;
      }
      this._element.requestVideoFrameCallback(this.processVideoMetadata.bind(this));
      this._player._channels.input.queueMetadataFrame({
        serverDataKey: data.rtpTimestamp,
        firstFramePacketArrivalTimeMs: data.receiveTime,
        frameSubmittedTimeMs: data.receiveTime,
        frameDecodedTimeMs: data.expectedDisplayTime,
        frameRenderedTimeMs: data.expectedDisplayTime
      });
    }
    getElement() {
      return this._element;
    }
    toggleDebugOverlay() {
      this._overlay.toggleDebug();
    }
    destroy() {
      const streamHolder = document.getElementById(this._player.getElementId());
      const element = streamHolder?.querySelector("video");
      if (this._overlay !== void 0) {
        this._overlay.destroy();
      }
      if (element) {
        element.remove();
      }
    }
  };

  // xcloud-player/src/lib/render/webgpu.ts
  var WebGpuComponent = class {
    constructor(player) {
      this._isActive = true;
      this._gpuVideoTexture = null;
      this._gpuDevice = null;
      this._gpuContext = null;
      this._gpuBindGroup = null;
      this._gpuPipeline = null;
      this._gpuSampler = null;
      this._resizeObserver = null;
      this._containerElement = null;
      this._isProcessingFrame = false;
      this._frameQueueSize = 0;
      this._droppedFrames = 0;
      this._lastFrameTime = 0;
      this._renderingFps = 0;
      this._frameTimeSamples = [];
      this._renderDelayMs = 0;
      this._renderDelaySamples = [];
      this._player = player;
      this._overlay = new Overlay(this, this._player);
    }
    create(stream) {
      const videoElement = document.createElement("canvas");
      console.log("Creating WebGPU canvas element for stream:", stream);
      videoElement.width = 1920;
      videoElement.height = 1080;
      videoElement.style.willChange = "contents";
      const that = this;
      navigator.gpu.requestAdapter().then((adapter) => {
        if (!adapter) {
          console.error("Failed to get GPU adapter");
          return;
        }
        console.log("WebGPU adapter:", adapter);
        adapter.requestDevice().then((device) => {
          const context = videoElement.getContext("webgpu");
          if (!context) {
            console.error("Failed to get WebGPU context");
            return;
          }
          const format = navigator.gpu.getPreferredCanvasFormat();
          context.configure({
            device,
            format,
            alphaMode: "opaque",
            // Enable desynchronized mode for lower latency and reduced jitter
            usage: GPUTextureUsage.RENDER_ATTACHMENT
          });
          this._gpuDevice = device;
          this._gpuContext = context;
          that._gpuVideoTexture = this._gpuDevice.createTexture({
            size: [videoElement.width, videoElement.height],
            format: "rgba8unorm",
            usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST | GPUTextureUsage.RENDER_ATTACHMENT
          });
          const vertexModule = this._gpuDevice.createShaderModule({
            code: `
                        struct VertexOutput {
                            @builtin(position) position: vec4<f32>,
                            @location(0) uv: vec2<f32>,
                        }

                        @vertex
                        fn main(@builtin(vertex_index) VertexIndex: u32) -> VertexOutput {
                            var pos = array<vec2<f32>, 4>(
                                vec2<f32>(-1.0, -1.0),
                                vec2<f32>( 1.0, -1.0),
                                vec2<f32>(-1.0,  1.0),
                                vec2<f32>( 1.0,  1.0)
                            );
                            var uv = array<vec2<f32>, 4>(
                                vec2<f32>(0.0, 1.0),
                                vec2<f32>(1.0, 1.0),
                                vec2<f32>(0.0, 0.0),
                                vec2<f32>(1.0, 0.0)
                            );
                            var output: VertexOutput;
                            output.position = vec4<f32>(pos[VertexIndex], 0.0, 1.0);
                            output.uv = uv[VertexIndex];
                            return output;
                        }
                    `
          });
          const fragmentModule = this._gpuDevice.createShaderModule({
            code: `
                        @group(0) @binding(0) var myTexture: texture_2d<f32>;
                        @group(0) @binding(1) var mySampler: sampler;

                        @fragment
                        fn main(@location(0) uv: vec2<f32>) -> @location(0) vec4<f32> {
                            // Get texture dimensions
                            let texSize = vec2<f32>(textureDimensions(myTexture));
                            let invTexSize = 1.0 / texSize;
                            
                            // Edge-aware upscaling with Catmull-Rom interpolation
                            // Sample 3x3 neighborhood for edge detection
                            let p00 = textureSample(myTexture, mySampler, uv + vec2<f32>(-1.0, -1.0) * invTexSize).rgb;
                            let p10 = textureSample(myTexture, mySampler, uv + vec2<f32>( 0.0, -1.0) * invTexSize).rgb;
                            let p20 = textureSample(myTexture, mySampler, uv + vec2<f32>( 1.0, -1.0) * invTexSize).rgb;
                            
                            let p01 = textureSample(myTexture, mySampler, uv + vec2<f32>(-1.0,  0.0) * invTexSize).rgb;
                            let p11 = textureSample(myTexture, mySampler, uv).rgb;
                            let p21 = textureSample(myTexture, mySampler, uv + vec2<f32>( 1.0,  0.0) * invTexSize).rgb;
                            
                            let p02 = textureSample(myTexture, mySampler, uv + vec2<f32>(-1.0,  1.0) * invTexSize).rgb;
                            let p12 = textureSample(myTexture, mySampler, uv + vec2<f32>( 0.0,  1.0) * invTexSize).rgb;
                            let p22 = textureSample(myTexture, mySampler, uv + vec2<f32>( 1.0,  1.0) * invTexSize).rgb;
                            
                            // Calculate Sobel edge detection
                            let edgeX = -p00 + p20 - 2.0*p01 + 2.0*p21 - p02 + p22;
                            let edgeY = p00 + 2.0*p10 + p20 - p02 - 2.0*p12 - p22;
                            let edge = length(edgeX) + length(edgeY);
                            
                            // Sample with adaptive filtering based on edge strength
                            let centerSample = textureSample(myTexture, mySampler, uv);
                            var resultRgb = centerSample.rgb;
                            
                            // For smooth areas, apply slight smoothing
                            // For edge areas, keep sharp sampling
                            if (edge < 0.1) {
                                // Smooth region: blend neighboring pixels for smoothness
                                let neighbors = (p10 + p01 + p21 + p12) * 0.25;
                                resultRgb = mix(resultRgb, neighbors, 0.15);
                            } else {
                                // Edge region: apply subtle sharpening to maintain clarity
                                resultRgb = resultRgb * 1.1 - (p10 + p01 + p21 + p12) * 0.025;
                            }
                            
                            return vec4<f32>(resultRgb, centerSample.a);
                        }
                    `
          });
          this._gpuPipeline = this._gpuDevice.createRenderPipeline({
            vertex: {
              module: vertexModule,
              entryPoint: "main"
            },
            fragment: {
              module: fragmentModule,
              entryPoint: "main",
              targets: [{ format: navigator.gpu.getPreferredCanvasFormat() }]
            },
            primitive: { topology: "triangle-strip" },
            layout: "auto"
          });
          this._gpuSampler = this._gpuDevice.createSampler({
            magFilter: "linear",
            minFilter: "linear",
            addressModeU: "clamp-to-edge",
            addressModeV: "clamp-to-edge"
          });
          const track = stream.getVideoTracks()[0];
          const processor = new MediaStreamTrackProcessor({ track });
          this._gpuReader = processor.readable.getReader();
          this._gpuBindGroup = this._gpuDevice.createBindGroup({
            layout: this._gpuPipeline.getBindGroupLayout(0),
            entries: [
              { binding: 0, resource: this._gpuVideoTexture.createView() },
              { binding: 1, resource: this._gpuSampler }
            ]
          });
          that.processVideoData();
        });
      });
      const element = document.getElementById(this._player.getElementId());
      if (element === null) {
        return;
      }
      this._element = videoElement;
      this._containerElement = element;
      element.appendChild(this._element);
      const isStatic = getComputedStyle(element).position === "static" ? true : false;
      if (isStatic === true) {
        element.style.position = "relative";
      }
      this._element.style.width = "100%";
      this._element.style.height = "100%";
      this._element.style.display = "block";
      this._resizeObserver = new ResizeObserver(() => {
        this.resizeCanvas();
      });
      this._resizeObserver.observe(element);
      this.resizeCanvas();
    }
    async processVideoData(timestamp) {
      if (this._isProcessingFrame || !this._isActive) {
        return;
      }
      this._isProcessingFrame = true;
      try {
        const { value: frame, done } = await this._gpuReader.read();
        if (done || !this._isActive || !frame) {
          this._isProcessingFrame = false;
          return;
        }
        if (this._frameQueueSize > 0) {
          frame.close();
          this._droppedFrames++;
          this._isProcessingFrame = false;
          this.videoLoop();
          return;
        }
        this._frameQueueSize++;
        if (this._gpuVideoTexture && (this._gpuVideoTexture.width !== frame.codedWidth || this._gpuVideoTexture.height !== frame.codedHeight)) {
          this._gpuVideoTexture.destroy();
          this._gpuVideoTexture = this._gpuDevice.createTexture({
            size: [frame.codedWidth, frame.codedHeight],
            format: "rgba8unorm",
            usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST | GPUTextureUsage.RENDER_ATTACHMENT
          });
          this._gpuBindGroup = this._gpuDevice.createBindGroup({
            layout: this._gpuPipeline.getBindGroupLayout(0),
            entries: [
              { binding: 0, resource: this._gpuVideoTexture.createView() },
              { binding: 1, resource: this._gpuSampler }
            ]
          });
          console.log(`Video texture resized to ${frame.codedWidth}x${frame.codedHeight}`);
        }
        const renderStartTime = performance.now();
        if (this._lastFrameTime > 0) {
          const frameTime = renderStartTime - this._lastFrameTime;
          this._frameTimeSamples.push(frameTime);
          if (this._frameTimeSamples.length > 60) {
            this._frameTimeSamples.shift();
          }
          const avgFrameTime = this._frameTimeSamples.reduce((a, b) => a + b, 0) / this._frameTimeSamples.length;
          this._renderingFps = Math.round(1e3 / avgFrameTime);
        }
        this._lastFrameTime = renderStartTime;
        this._gpuDevice.queue.copyExternalImageToTexture(
          { source: frame },
          { texture: this._gpuVideoTexture },
          [frame.codedWidth, frame.codedHeight]
        );
        const commandEncoder = this._gpuDevice.createCommandEncoder();
        const passEncoder = commandEncoder.beginRenderPass({
          colorAttachments: [{
            view: this._gpuContext.getCurrentTexture().createView(),
            loadOp: "clear",
            storeOp: "store",
            clearValue: { r: 0, g: 0, b: 0, a: 1 }
          }]
        });
        passEncoder.setPipeline(this._gpuPipeline);
        passEncoder.setBindGroup(0, this._gpuBindGroup);
        passEncoder.draw(4);
        passEncoder.end();
        this._gpuDevice.queue.submit([commandEncoder.finish()]);
        const renderEndTime = performance.now();
        const renderDelay = renderEndTime - renderStartTime;
        this._renderDelaySamples.push(renderDelay);
        if (this._renderDelaySamples.length > 60) {
          this._renderDelaySamples.shift();
        }
        this._renderDelayMs = Math.round(
          this._renderDelaySamples.reduce((a, b) => a + b, 0) / this._renderDelaySamples.length * 100
        ) / 100;
        this._player._channels.input.queueMetadataFrame({
          serverDataKey: frame.timestamp,
          firstFramePacketArrivalTimeMs: frame.timestamp,
          frameSubmittedTimeMs: renderStartTime,
          frameDecodedTimeMs: renderStartTime,
          frameRenderedTimeMs: renderEndTime
        });
        frame.close();
        this._frameQueueSize--;
      } catch (error) {
        console.error("Error processing video frame:", error);
      } finally {
        this._isProcessingFrame = false;
        if (this._isActive) {
          this.videoLoop();
        }
      }
    }
    videoLoop() {
      this.processVideoData();
    }
    resizeCanvas() {
      if (!this._element || !this._containerElement || !this._gpuContext || !this._gpuDevice) {
        return;
      }
      const displayWidth = this._containerElement.clientWidth;
      const displayHeight = this._containerElement.clientHeight;
      const devicePixelRatio = window.devicePixelRatio || 1;
      const width = Math.floor(displayWidth * devicePixelRatio);
      const height = Math.floor(displayHeight * devicePixelRatio);
      if (this._element.width !== width || this._element.height !== height) {
        this._element.width = width;
        this._element.height = height;
        const format = navigator.gpu.getPreferredCanvasFormat();
        this._gpuContext.configure({
          device: this._gpuDevice,
          format,
          alphaMode: "opaque",
          usage: GPUTextureUsage.RENDER_ATTACHMENT
        });
        console.log(`Canvas resized to ${width}x${height} (display: ${displayWidth}x${displayHeight}, DPR: ${devicePixelRatio})`);
      }
    }
    // processVideoMetadata(timestamp, data:VideoFrameCallbackMetadata) {
    //     if(this._element === undefined) {return}
    //     this._element.requestVideoFrameCallback(this.processVideoMetadata.bind(this))
    //     this._player._channels.input.queueMetadataFrame({
    //         serverDataKey: data.rtpTimestamp as number,
    //         firstFramePacketArrivalTimeMs: data.receiveTime as number,
    //         frameSubmittedTimeMs: data.receiveTime as number,
    //         frameDecodedTimeMs: data.expectedDisplayTime,
    //         frameRenderedTimeMs: data.expectedDisplayTime,
    //     })
    // }
    getElement() {
      return this._element;
    }
    toggleDebugOverlay() {
      this._overlay.toggleDebug();
    }
    getFrameStats() {
      return {
        droppedFrames: this._droppedFrames,
        queueSize: this._frameQueueSize,
        isProcessing: this._isProcessingFrame,
        renderingFps: this._renderingFps,
        renderDelayMs: this._renderDelayMs
      };
    }
    destroy() {
      const streamHolder = document.getElementById(this._player.getElementId());
      this._isActive = false;
      const element = streamHolder?.querySelector("canvas");
      if (this._gpuReader) {
        this._gpuReader.cancel();
        this._gpuReader = void 0;
      }
      if (this._resizeObserver) {
        this._resizeObserver.disconnect();
        this._resizeObserver = null;
      }
      if (this._gpuVideoTexture) {
        this._gpuVideoTexture.destroy();
        this._gpuVideoTexture = null;
      }
      if (this._overlay !== void 0) {
        this._overlay.destroy();
      }
      if (element) {
        element.remove();
      }
      if (this._element) {
        this._element.remove();
      }
    }
  };

  // xcloud-player/src/lib/render/audio.ts
  var AudioComponent = class {
    constructor(player) {
      this._player = player;
    }
    create(stream) {
      const audioElement = document.createElement("audio");
      audioElement.srcObject = stream;
      audioElement.autoplay = true;
      audioElement.muted = false;
      this._element = audioElement;
      const element = document.getElementById(this._player.getElementId());
      if (element === null) {
        return;
      }
      element.appendChild(this._element);
    }
    getElement() {
      return this._element;
    }
    destroy() {
      const streamHolder = document.getElementById(this._player.getElementId());
      const element = streamHolder?.querySelector("audio");
      if (element) {
        element.remove();
      }
    }
  };

  // xcloud-player/src/lib/player.ts
  var xCloudPlayer = class {
    constructor(elementId) {
      this._peerConnection = new RTCPeerConnection({});
      this._channels = {
        chat: new ChatChannel(this),
        control: new ControlChannel(this),
        input: new InputChannel(this),
        message: new MessageChannel(this)
      };
      this._isDestoyed = false;
      this._iceHelper = new Ice(this);
      this._sdpHelper = new Sdp(this);
      this._statsHelper = new Stats(this);
      console.log("xCloudPlayer constructing...");
      this._elementId = elementId;
      this._peerConnection.addTransceiver("audio", { direction: "sendrecv" });
      const videoTransceiver = this._peerConnection.addTransceiver("video", { direction: "recvonly" });
      videoTransceiver.setCodecPreferences(this._sdpHelper.getDefaultCodecPreferences());
      this._peerConnection.ontrack = (event) => {
        if (event.track.kind === "video") {
          this._videoComponent = navigator.gpu && location.search.includes("webgpu") ? new WebGpuComponent(this) : new VideoComponent(this);
          this._videoComponent.create(event.streams[0]);
        } else if (event.track.kind === "audio") {
          this._audioComponent = new AudioComponent(this);
          this._audioComponent.create(event.streams[0]);
        } else {
          console.log("[xPlayer] Detected an unknown stream type: ", event.track.kind);
        }
      };
      console.log("xCloudPlayer constructed.");
    }
    init() {
      console.log("xCloudPlayer init called.");
    }
    getElementId() {
      return this._elementId;
    }
    async createOffer() {
      const offer = await this._peerConnection.createOffer({
        offerToReceiveAudio: true,
        offerToReceiveVideo: true
      });
      const finalOffer = this._sdpHelper.setLocalSDP(offer);
      this._peerConnection.setLocalDescription(finalOffer);
      return finalOffer;
    }
    async setRemoteSDP(sdp) {
      const finalSdp = this._sdpHelper.setRemoteSDP(sdp);
      return await this._peerConnection.setRemoteDescription({
        type: "answer",
        sdp: finalSdp
      });
    }
    async getICECandidates() {
      return this._iceHelper.getCandidates();
    }
    async setRemoteIceCandidates(candidates) {
      this._iceHelper.setRemoteCandidates(candidates);
    }
    getVideoElement() {
      if (this._videoComponent) {
        return this._videoComponent.getElement();
      } else {
        return void 0;
      }
    }
    getAudioElement() {
      if (this._audioComponent) {
        return this._audioComponent.getElement();
      } else {
        return void 0;
      }
    }
    toggleDebugOverlay() {
      if (this._videoComponent) {
        this._videoComponent.toggleDebugOverlay();
      }
    }
    getStats() {
      return this._statsHelper;
    }
    destroy() {
      if (this._isDestoyed === false) {
        this._peerConnection.close();
        if (this._peerConnection.onconnectionstatechange) {
          this._peerConnection.onconnectionstatechange(new Event("connectionstatechanged"));
        }
        for (const channel in this._channels) {
          this._channels[channel].destroy();
        }
        if (this._videoComponent) {
          this._videoComponent.destroy();
        }
        if (this._audioComponent) {
          this._audioComponent.destroy();
        }
        this._isDestoyed = true;
      } else {
        console.log("Cannot destroy because the player is already destroyed.");
      }
    }
  };

  // xcloud-player/src/entry.ts
  var statusEl = document.getElementById("status");
  var shown = "";
  var show = (msg, error = false) => {
    statusEl.textContent = msg;
    statusEl.className = error ? "error" : "";
    statusEl.style.display = msg ? "block" : "none";
    if (msg && msg !== shown) void fetch("/api/log", { method: "POST", body: (error ? "ERROR " : "") + msg });
    shown = msg;
  };
  var sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function api(path, body) {
    const r = await fetch("/api/" + path, {
      method: body === void 0 ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === void 0 ? void 0 : JSON.stringify(body)
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.message || `${path} failed (HTTP ${r.status})`);
    return j;
  }
  async function waitForConsole() {
    let connected = false;
    for (; ; ) {
      const s = await api("state");
      if (s.state === "Provisioned") return;
      if (s.state === "Failed") throw new Error(s.message || "Xbox Cloud Gaming could not start this game");
      if (s.state === "WaitingForResources") show("Waiting in the Xbox Cloud Gaming queue\u2026");
      else show("Starting your game\u2026");
      if (s.state === "ReadyToConnect" && !connected) {
        connected = true;
        await api("connect", {});
      }
      await sleep(1e3);
    }
  }
  async function waitForHandshake(player) {
    const channels = player._channels;
    for (let i = 0; i < 300 && !channels.message?._handshakeCompleted; i++) await sleep(100);
    if (!channels.message?._handshakeCompleted) {
      void fetch("/api/log", { method: "POST", body: "ERROR stream handshake never completed" });
    }
    await sleep(500);
  }
  async function run() {
    show("Starting your game\u2026");
    await api("start", {});
    await waitForConsole();
    const player = new xCloudPlayer("playerContainer");
    player.init();
    const offer = await player.createOffer();
    const sdp = await api("sdp", { sdp: offer.sdp });
    await player.setRemoteSDP(JSON.parse(sdp.exchangeResponse).sdp);
    const local = (await player.getICECandidates()).map((c) => JSON.stringify({
      candidate: c.candidate,
      sdpMid: c.sdpMid,
      sdpMLineIndex: c.sdpMLineIndex,
      usernameFragment: c.usernameFragment
    }));
    const ice = await api("ice", { candidates: local });
    await player.setRemoteIceCandidates(JSON.parse(ice.exchangeResponse));
    await waitForHandshake(player);
    const pad = new Gamepad(0, { enable_keyboard: true });
    pad.attach(player);
    void fetch("/api/log", { method: "POST", body: "controller attached" });
    setInterval(() => void api("keepalive", {}).catch(() => void 0), 3e4);
    show("");
    void fetch("/api/log", { method: "POST", body: "streaming" });
    let last = "";
    setInterval(() => {
      const pads = [...navigator.getGamepads()].filter(Boolean).map((p) => `${p.id} (${p.mapping || "no mapping"})`);
      const now = pads.join(", ") || "none";
      if (now !== last) {
        last = now;
        void fetch("/api/log", { method: "POST", body: "gamepads: " + now });
      }
    }, 1e3);
  }
  run().catch((e) => show(String(e?.message ?? e), true));
})();
