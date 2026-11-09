export default class Teredo {
    private _address:string

    private _ipv4Address:string
    private _ipv4Port:number

    constructor(address:string){
        this._address = address

        const split_str = this._address.split(':')
        const ipv4_part = split_str[6] + split_str[7]
        const ipv4_port_part = split_str[5]

        const ip_1 =~ parseInt(ipv4_part.substring(0, 2), 16) & 0xFF
        const ip_2 =~ parseInt(ipv4_part.substring(2, 4), 16) & 0xFF
        const ip_3 =~ parseInt(ipv4_part.substring(4, 6), 16) & 0xFF
        const ip_4 =~ parseInt(ipv4_part.substring(6, 8), 16) & 0xFF
        const port_1 =~ parseInt(ipv4_port_part.substring(0, 4), 16) & 0xFFFF

        this._ipv4Address = ip_1+'.'+ip_2+'.'+ip_3+'.'+ip_4
        this._ipv4Port = port_1
    }

    getIpv4Address(){
        return this._ipv4Address
    }

    getIpv4Port(){
        return this._ipv4Port
    }
}