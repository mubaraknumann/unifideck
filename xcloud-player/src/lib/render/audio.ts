import xCloudPlayer from '../player'

export default class AudioComponent {
    private _player:xCloudPlayer

    private _element:HTMLAudioElement | undefined

    constructor(player:any){
        this._player = player
    }

    create(stream:MediaStream) {
        const audioElement = document.createElement('audio')
        audioElement.srcObject = stream
        audioElement.autoplay = true
        audioElement.muted = false

        this._element = audioElement

        const element = document.getElementById(this._player.getElementId())
        if(element === null) {return}

        element.appendChild(this._element)
    }

    getElement(){
        return this._element
    }

    destroy(){
        const streamHolder = document.getElementById(this._player.getElementId())
        const element = streamHolder?.querySelector('audio')

        if(element){
            element.remove()
        }
    }
}