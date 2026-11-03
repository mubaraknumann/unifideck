import Channel from './channel'

export default class DebugChannel extends Channel {
    getChannelName() {
        return 'debug'
    }

    getChannelConfig() {
        return { }
    }

    destroy() {
        // console.log('DebugChannel destroy() called')
    }
}