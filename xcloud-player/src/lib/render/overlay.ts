import xCloudPlayer from '../player'
import VideoComponent from './video'
import WebGPUComponent from './webgpu'

interface OverlayInterface {
    debug: undefined | HTMLElement;
}

export default class Overlay {
    private _player:xCloudPlayer
    private _videoComponent:VideoComponent | WebGPUComponent

    private _overlays:OverlayInterface = {
        debug: undefined,
    }

    constructor(videoComponent:VideoComponent | WebGPUComponent, player:xCloudPlayer){
        this._videoComponent = videoComponent
        this._player = player
    }

    toggleDebug(){
        if(this._overlays.debug === undefined){
            this.createDebugOverlay()
        } else {
            this.destroy()
        }
    }

    createDebugOverlay(){
        this._overlays.debug = document.createElement('div')
        this._overlays.debug.id = 'playerDebugOverlay'

        this._overlays.debug.style.position = 'absolute'
        this._overlays.debug.style.top = '0'
        this._overlays.debug.style.left = '0'
        this._overlays.debug.style.width = this._videoComponent.getElement()?.style.width || '100%'
        this._overlays.debug.style.height = this._videoComponent.getElement()?.style.height || 'auto'
        this._overlays.debug.style.background = 'linear-gradient(0deg, rgba(0,0,0, 0) 0%, rgba(0,0,0, 0) 75%, rgba(0,0,0, 0.5) 100%)'
        this._overlays.debug.style.padding = '10px'
        this._overlays.debug.style.pointerEvents = 'none'

        document.getElementById(this._player.getElementId())?.appendChild(this._overlays.debug)

        setTimeout(() => {
            this.refreshDebugOverlay()
        }, 500)
        this.refreshDebugOverlay()
    }

    destroy(){
        this._overlays.debug?.remove()
        this._overlays.debug = undefined
    }

    refreshDebugOverlay(){
        if(this._overlays.debug !== undefined){
            this._overlays.debug.innerHTML = ''

            this._overlays.debug.appendChild(this.createLabel('Local Play', this._player.getStats()._remoteIsLocal ? 'Local' : 'Remote', 'ok'))
            this._overlays.debug.appendChild(this.createLabel('Resolution', this._player.getStats()._videoWidth+'x'+this._player.getStats()._videoHeight, 'ok'))
            this._overlays.debug.appendChild(this.createLabel('Stream FPS', this._player.getStats()._videoFps.toString(), this._player.getStats()._videoFps >= 58 ? 'ok' : 'warning'))
            
            // Display rendering FPS if available
            const frameStats = (this._videoComponent as any).getFrameStats?.();
            if (frameStats) {
                this._overlays.debug.appendChild(this.createLabel('Render FPS', frameStats.renderingFps.toString(), frameStats.renderingFps >= 58 ? 'ok' : 'warning'))
                this._overlays.debug.appendChild(this.createLabel('Render Delay', frameStats.renderDelayMs.toString() + ' ms', frameStats.renderDelayMs < 16 ? 'ok' : frameStats.renderDelayMs < 33 ? 'warning' : 'error'))
                this._overlays.debug.appendChild(this.createLabel('Dropped Frames', frameStats.droppedFrames.toString(), frameStats.droppedFrames === 0 ? 'ok' : frameStats.droppedFrames < 30 ? 'warning' : 'error'))
            }
            
            this._overlays.debug.appendChild(this.createLabel('Connection', this._player.getStats()._remoteIsIpv6 ? 'IPv6' : 'IPv4', 'ok'))
            this._overlays.debug.appendChild(this.createLabel('Video', this._player.getStats()._videoCodec, 'ok'))
            const rttButtonState = (this._player.getStats()._rtt*1000 > 75) ? 'error' : (this._player.getStats()._rtt*1000 > 40) ? 'warning' : 'ok'
            this._overlays.debug.appendChild(this.createLabel('RTT', (this._player.getStats()._rtt*1000).toString()+' ms', rttButtonState))
            
            setTimeout(() => {
                this.refreshDebugOverlay()
            }, 500)
        }
    }

    createLabel(text:string, value:string = '', style:string = ''){
        const labelStyle = {
            borderRadius: '5px',
            background: 'linear-gradient(0deg, rgba(28,26,26,1) 0%, rgba(47,45,45,1) 100%)',
            padding: '10px',
            fontSize: '12px',
            marginTop: '3px',
            marginRight: '10px',
            marginBottom: '2px',
            textTransform: 'uppercase',
            color: 'white',
            display: 'inline-block',
        }

        const element = document.createElement('span')

        const colors = {
            'error': 'rgb(220, 53, 69)',
            'warning': 'rgb(255, 193, 7)',
            'ok': 'rgb(25, 135, 84)',
        }

        if(value !== ''){
            element.innerHTML = `${text}: <span style="background: ${colors[style]}; border-radius: 5px; padding: 4px; padding-left: 8px; padding-right: 8px;">${value}</span>`
        } else {
            element.innerText = text
        }
            

        for(const key in labelStyle){
            element.style[key] = labelStyle[key]
        }
        return element
    }
    
}