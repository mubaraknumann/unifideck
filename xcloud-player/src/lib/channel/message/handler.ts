import Channel from '../../lib/channel'

export default class MessageHandler {
    private _channel:Channel
    private _data:object

    constructor(channel:Channel, data:object) {
        this._channel = channel
        this._data = data

        this.handle(this._data)
    }

    handle(data) {
        console.log('[MessageHandler] Handling:', data)
        switch(data.target){
            case '/streaming/sessionLifetimeManagement/serverInitiatedDisconnect':
                this.completeTransaction(data.id, '')
                this._channel.getPlayer().destroy()
                break
            
            case '/streaming/properties/titleinfo':
                console.log('Title update:', data)
                break
            
            case '/streaming/touchcontrols/showlayoutv2':
                // console.log('Showing touch layout:', data)
                break

            case '/streaming/systemUi/messages/ShowMessageDialog':
                this.handleMessage(data)
                break

            default:
                console.log('[MessageChannel] Unhandled transaction:', data)
        }

    }

    completeTransaction(id, data) {
        const transaction = JSON.stringify({
            'type': 'TransactionComplete',
            'content': JSON.stringify(data),
            // 'content':'{\'Result\':0}',
            'id': id,
            'cv': '',
        })

        this._channel.send(transaction)
    }

    cancelTransaction(id, data) {
        const transaction = JSON.stringify({
            'type': 'ReceiverCancel',
            'content': JSON.stringify(data),
            'id': id,
            'cv': '',
        })

        this._channel.send(transaction)
    }

    handleMessage(data) {
        const jsonData = JSON.parse(data.content)
        console.log(jsonData)
        if(confirm(jsonData.TitleText+'\n\n'+jsonData.ContentText)){
            this.completeTransaction(data.id, { Result: 0 })
        } else {
            this.completeTransaction(data.id, { Result: 1 })
            // this.cancelTransaction(data.id, '')
        }
    }
}