#include "Vmmio_store_transaction.h"
#include "verilated.h"
#include "packed_bits.hpp"
#include "fetch_memory_layout.hpp"
#include <array>
#include <bitset>
#include <cstdint>
#include <iostream>
#include <map>
#include <random>
#include <stdexcept>
#include <string>
using packed_bits::put;

struct Store { unsigned id=0, size=0, cause=0; uint32_t address=0, raw=0; };
struct Input {
    Store desc;
    bool reset=false, kill=false, launch=false, allow=false, head=false;
    bool request_ready=false, response=false, take=false;
    unsigned head_id=0, response_id=0, status=0, payload=0, bit=0;
};
struct Bench {
    Vmmio_store_transaction d;
    std::mt19937 rng;
    Store held;
    bool owned=false, authorized=false, sent=false, result=false;
    unsigned fault=0, fatal=0, transaction=0, cycles=0, cases=0;
    std::map<std::string,unsigned> count;
    std::bitset<4> id_bits;
    std::bitset<256> line_bits;
    std::bitset<32> word_bits;
    explicit Bench(unsigned seed): rng(seed) {}
    void require(bool ok,const std::string& why) const {
        if (!ok) throw std::runtime_error("mmio store mismatch cycle="+std::to_string(cycles)+": "+why);
    }
    Store make(unsigned id,unsigned size,uint32_t address,unsigned cause=0) {
        return {id,size,cause,address,uint32_t(rng())};
    }
    Input input(Store s) {
        Input i; i.desc=s; i.head_id=s.id; i.response_id=transaction; return i;
    }
    Input noise() {
        auto i=input(make(rng()%8192,rng()%3,rng()));
        i.allow=rng()%2; i.head=rng()%2; i.head_id=rng()%8192; return i;
    }
    void drive(Input i) {
        d.clk_i=0; d.rst_i=i.reset; d.kill_i=i.kill; d.launch_i=i.launch;
        d.id_i=i.desc.id; d.address_i=i.desc.address; d.size_i=i.desc.size;
        d.store_i=1; d.uncached_i=!i.desc.cause; d.fault_i=i.desc.cause!=0;
        d.cause_i=i.desc.cause; d.trap_value_i=i.desc.cause ? i.desc.address:0;
        unsigned mask=i.desc.size==0 ? 1:i.desc.size==1 ? 3:15;
        uint32_t bits=i.desc.size==0 ? 255:i.desc.size==1 ? 65535:0xffffffffU;
        d.byte_mask_i=i.desc.cause ? 0:mask<<(i.desc.address%4);
        d.write_data_i=i.desc.cause ? 0:(i.desc.raw&bits)<<(8*(i.desc.address%4));
        d.issue_allowed_i=i.allow; d.head_valid_i=i.head; d.head_id_i=i.head_id;
        d.request_ready_i=i.request_ready; d.response_valid_i=i.response; d.take_i=i.take;
        for (unsigned n=0;n<(RESPONSE_BITS+31)/32;++n) d.response_i[n]=0;
        put(d.response_i,RESPONSE_TRANSACTION_ID_OFFSET,4,i.response_id);
        put(d.response_i,RESPONSE_STATUS_OFFSET,2,i.status);
        if (i.payload) put(d.response_i,(i.payload==1 ? RESPONSE_LINE_READ_DATA_OFFSET:RESPONSE_UNCACHED_READ_DATA_OFFSET)+i.bit,1,1);
    }
    void step(Input i) {
        drive(i); d.eval();
        bool live=!i.reset && !fatal, irrev=!i.reset && owned && authorized;
        bool ready=live && !i.kill && !i.response && !owned;
        bool grant=live && owned && !authorized && !result && !i.kill && !i.response
            && i.allow && i.head && i.head_id==held.id;
        bool offer=live && owned && authorized && !sent;
        bool valid=live && !i.response && result && (!i.kill || irrev);
        require(bool(d.ready_o)==ready,"descriptor capacity");
        require(bool(d.authorize_o)==grant,"head authorization");
        require(bool(d.busy_o)==(!i.reset && owned) && d.owner_id_o==(!i.reset && owned ? held.id:0),"owned identity");
        require(bool(d.irrevocable_o)==irrev,"irrevocable lifetime");
        require(bool(d.request_valid_o)==offer,"request ownership");
        require(bool(d.response_ready_o)==(live && sent && !result),"response ownership");
        require(bool(d.valid_o)==valid,"held result");
        if (!i.reset) require(bool(d.fatal_o)==bool(fatal) && d.fatal_reason_o==fatal,"sticky fatal reason");
        uint32_t bytes=0; unsigned mask=0;
        if (owned && !held.cause) for (unsigned n=0;n<(1U<<held.size);++n) {
            unsigned lane=(held.address%4)+n;
            require(lane<4,"reference byte bounds");
            bytes|=((held.raw>>(8*n))&255U)<<(8*lane); mask|=1U<<lane;
        }
        require(d.id_o==(valid ? held.id:0) && d.address_o==(valid ? held.address:0)
            && d.size_o==(valid ? held.size:0),"result descriptor");
        require(d.write_data_o==(valid && !fault ? bytes:0) && d.byte_mask_o==(valid && !fault ? mask:0),"result bytes");
        require(bool(d.fault_o)==(valid && fault) && d.cause_o==(valid ? fault:0)
            && d.trap_value_o==(valid && fault ? held.address:0),"precise store fault");
        std::array<uint32_t,(REQUEST_BITS+31)/32> packet{};
        if (offer) {
            put(packet,REQUEST_TRANSACTION_ID_OFFSET,4,transaction);
            put(packet,REQUEST_WRITE_OFFSET,1,1); put(packet,REQUEST_UNCACHED_OFFSET,1,1);
            put(packet,REQUEST_ADDRESS_OFFSET,32,held.address); put(packet,REQUEST_UNCACHED_SIZE_OFFSET,2,held.size);
            put(packet,REQUEST_UNCACHED_WRITE_DATA_OFFSET,32,bytes); put(packet,REQUEST_UNCACHED_WRITE_STROBE_OFFSET,4,mask);
        }
        for (unsigned n=0;n<REQUEST_BITS;++n)
            require(packed_bits::bit(d.request_o,n)==packed_bits::bit(packet,n),"request packet/stability");
        unsigned error=!i.response ? 0:!sent || result ? 1:i.response_id!=transaction ? 2:i.status>1 ? 3:i.payload ? 4:0;
        d.clk_i=1; d.eval(); ++cycles;
        if (i.reset) { owned=authorized=sent=result=false; fault=fatal=transaction=0; ++count["reset"]; }
        else if (!fatal) {
            if (error) { fatal=error; ++count["fatal_"+std::to_string(error)]; }
            else if (i.kill && owned && !authorized) {
                ++count[result ? "kill_local":"kill_wait"]; owned=result=false;
            } else if (i.take && valid) {
                ++count[fault ? "trap_take":"success_take"]; owned=authorized=sent=result=false;
            } else if (i.response) {
                result=true; fault=i.status==1 ? 7:0;
                transaction=(transaction+1)%16; ++count["responses"];
                if (!transaction) ++count["id_wrap"];
            } else if (offer && i.request_ready) { sent=true; ++count["requests"]; }
            else if (grant) { authorized=true; ++count["authorized"]; }
            else if (i.launch && ready) {
                held=i.desc; owned=true; authorized=sent=false; result=held.cause!=0; fault=held.cause;
                ++cases; ++count["launched"];
            }
        }
    }
    void reset() { Input i; i.reset=true; step(i); step(input({})); }
    void launch(Store s) { auto i=input(s); i.launch=true; step(i); }
    void authorize(Store s) { auto i=noise(); i.head_id=s.id; i.desc.id=s.id^8191; i.allow=i.head=true; step(i); }
    void qualifiers() {
        auto s=make(8191,0,0x10000003); launch(s);
        auto i=input(s); i.head=true; step(i); ++count["permission_block"];
        i.allow=true; i.head=false; step(i); ++count["head_block"];
        i.head=true;
        for (unsigned bit=0;bit<13;++bit) { i.head_id=s.id^(1U<<bit); i.desc.id=i.head_id; step(i); ++count["head_bit_"+std::to_string(bit)]; }
        i=input(s); i.allow=i.head=i.kill=true; step(i); ++count["kill_grant_collision"];
        step(i); ++count["idle_kill"];
        step(input(s));
    }
    void run(Store s,bool fast,bool permissions,bool access_fault) {
        launch(s); auto i=input(s);
        unsigned waits=fast ? 0:1+rng()%4;
        for (unsigned n=0;n<waits;++n) { step(i); ++count["head_wait"]; }
        ++count[fast ? "first_authorization":"delayed_authorization"];
        authorize(s);
        unsigned stalls=fast ? 0:1+rng()%4;
        auto controls=[&]() { auto j=permissions ? input(s):noise(); j.head=j.allow=permissions; return j; };
        for (unsigned n=0;n<stalls;++n) { step(controls()); ++count["request_stall"]; }
        i=controls(); i.request_ready=true; step(i);
        for (unsigned n=0;n<stalls;++n) { step(controls()); ++count["response_stall"]; }
        i=controls(); i.response=true; i.response_id=transaction; i.status=access_fault; step(i);
        for (unsigned n=0;n<stalls;++n) { step(controls()); ++count["result_stall"]; }
        i=controls(); i.take=true; step(i);
        ++count[fast ? "fast":"delayed"]; ++count[permissions ? "permission_retained":"permission_dropped"];
        ++count[access_fault ? "access_fault":"ok"]; ++count["size_"+std::to_string(s.size)];
        ++count["offset_"+std::to_string(s.address%32)];
    }
    void local_faults() {
        for (unsigned cause=6;cause<=7;++cause) for (unsigned action=0;action<3;++action) {
            auto s=make(67,2,0x10000003,cause); launch(s); auto i=input(s);
            for (unsigned n=0;n<3;++n) {
                i=noise(); i.desc.id=s.id^8191; i.desc.address=~s.address;
                i.desc.cause=cause==6 ? 7:6; i.allow=i.head=true; step(i);
                ++count["local_hold_noise"];
            }
            i=input(s);
            i.take=action==0; i.kill=action==1; i.reset=action==2; step(i); step(input(s));
            ++count["local_"+std::to_string(cause)+"_"+std::to_string(action)];
        }
    }
    void protocol(unsigned kind,unsigned bit=0) {
        reset(); auto s=make(8191,0,0x10000003); auto i=input(s);
        if (kind!=0) launch(s);
        if (kind>=2) authorize(s);
        if (kind>=4 && kind!=13) { i.request_ready=true; step(i); }
        if (kind==9 || kind==10) { i=input(s); i.response=true; step(i); }
        if (kind==9) { i=input(s); i.take=true; step(i); }
        i=input(s); i.response=true; i.allow=i.head=true;
        if (kind==3) i.request_ready=true;
        if (kind==4) { i.response_id^=1U<<bit; id_bits.set(bit); }
        if (kind==5 || kind==12) i.status=2;
        if (kind==6) i.status=3;
        if (kind==7) { i.payload=1; i.bit=bit; i.status=bit%2; line_bits.set(bit); }
        if (kind==8) { i.payload=2; i.bit=bit; i.status=bit%2; word_bits.set(bit); }
        if (kind==11 || kind==13) { i.response_id^=1; i.status=2; i.payload=1; }
        if (kind==12) i.payload=1;
        step(i); require(fatal!=0,"malformed response must be fatal");
        i=input(s); i.allow=i.head=true; step(i); ++count["fatal_channels"];
        if (kind==1) { i.kill=true; step(i); ++count["fatal_kill"]; }
        for (unsigned n=0;n<3;++n) { auto j=noise(); j.request_ready=j.response=true; j.status=3; step(j); }
        ++count["bad_"+std::to_string(kind)]; reset();
    }
    void reset_phases() {
        for (unsigned phase=0;phase<5;++phase) {
            reset(); auto s=make(35,2,28); auto i=input(s);
            if (phase) launch(s);
            if (phase>=2) authorize(s);
            if (phase>=3) { i.request_ready=true; step(i); }
            if (phase>=4) { i=input(s); i.response=true; step(i); }
            i=input(s); i.reset=true; i.allow=i.head=true;
            i.launch=phase==0; i.request_ready=phase==2; i.response=phase==3; i.take=phase==4;
            step(i); step(input(s)); ++count["reset_phase_"+std::to_string(phase)];
        }
    }
    void negative(const std::string& name) {
        reset(); auto s=make(7,0,0x10000003); auto i=input(s);
        if (name.rfind("kill-",0)==0) {
            launch(s); authorize(s);
            if (name!="kill-request") { i.request_ready=true; step(i); }
            if (name=="kill-success" || name=="kill-fault") { i=input(s); i.response=true; i.status=name=="kill-fault"; step(i); }
            i=input(s); i.kill=true; drive(i);
        } else if (name=="take") { i.take=true; drive(i); }
        else {
            if (name=="capacity") launch(s);
            i.launch=true; drive(i);
            if (name=="class") d.store_i=0;
            else if (name=="cached") d.uncached_i=0;
            else if (name=="size") d.size_i=3;
            else if (name=="alignment") d.size_i=1;
            else if (name=="mask") d.byte_mask_i=0;
            else if (name=="data") d.write_data_i|=1;
            else if (name=="cause" || name=="tval") {
                d.fault_i=1; d.byte_mask_i=d.write_data_i=0;
                d.cause_i=name=="cause" ? 5:7; d.trap_value_i=name=="cause" ? s.address:0;
            } else if (name!="capacity") throw std::runtime_error("unknown negative");
        }
        d.eval(); d.clk_i=1; d.eval(); throw std::runtime_error("caller violation survived");
    }
};
int main(int argc,char** argv) {
    Verilated::commandArgs(argc,argv);
    try {
        if (argc==3 && std::string(argv[1])=="negative") { Bench b(1); b.negative(argv[2]); return 1; }
        unsigned seed=argc>1 ? std::stoul(argv[1]):1, random=argc>2 ? std::stoul(argv[2]):2000;
        Bench b(seed); b.reset(); b.qualifiers(); b.local_faults();
        for (unsigned size=0;size<3;++size) for (unsigned offset=0;offset<32;offset+=1U<<size)
            for (unsigned mode=0;mode<8;++mode) b.run(b.make(8191-offset,size,0x10000000+offset),mode&1,mode&2,mode&4);
        for (unsigned id=0;id<8192;++id) b.run(b.make(id,0,id),true,id&1,id&2);
        b.count["all_identities"]=8192;
        b.run(b.make(8191,2,0xfffffffc),false,false,false); ++b.count["address_end"];
        for (unsigned kind=0;kind<14;++kind) b.protocol(kind);
        for (unsigned bit=0;bit<4;++bit) b.protocol(4,bit);
        for (unsigned bit=0;bit<256;++bit) b.protocol(7,bit);
        for (unsigned bit=0;bit<32;++bit) b.protocol(8,bit);
        b.reset_phases();
        for (unsigned n=0;n<random;++n) {
            unsigned size=b.rng()%3; uint32_t address=b.rng()&~((1U<<size)-1);
            b.run(b.make(b.rng()%8192,size,address),b.rng()%2,b.rng()%2,b.rng()%2);
        }
        b.step(b.input({})); b.require(!b.owned && !b.fatal,"final drain");
        b.count["response_id_bits"]=b.id_bits.count(); b.count["line_payload_bits"]=b.line_bits.count(); b.count["word_payload_bits"]=b.word_bits.count();
        std::cout<<"MMIO STORE PASS seed="<<seed<<" cycles="<<b.cycles<<" cases="<<b.cases<<" random="<<random;
        for (const auto& c:b.count) std::cout<<" "<<c.first<<"="<<c.second;
        std::cout<<"\n";
    } catch (const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
