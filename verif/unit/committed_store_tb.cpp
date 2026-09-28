#include "Vcommitted_store.h"
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

struct Store { unsigned id=0, size=0, mask=1; uint32_t address=0, data=0; };
struct Input {
    Store desc;
    bool reset=false, recovery=false, candidate=true, store=true, fault=false, uncached=false;
    bool head=true, admit=true, commit=false, request_ready=false, response=false;
    unsigned head_id=0, admit_id=0, response_id=0, status=0, payload=0, payload_bit=0;
};
struct Bench {
    Vcommitted_store d;
    std::mt19937 rng;
    Store held;
    bool owned=false, accepted=false;
    unsigned fatal=0, transaction=0, cycles=0, cases=0;
    std::map<std::string,unsigned> count;
    std::bitset<4> response_id_bits;
    std::bitset<256> line_payload_bits;
    std::bitset<32> word_payload_bits;
    explicit Bench(unsigned seed): rng(seed) {}
    void require(bool good, const std::string& why) const {
        if (!good) throw std::runtime_error("committed store mismatch cycle="+std::to_string(cycles)+": "+why);
    }
    Store make(unsigned id, unsigned size, uint32_t address) {
        Store s{id,size,0,address,0};
        uint32_t raw=rng();
        for (unsigned n=0;n<(1U<<size);++n) {
            unsigned lane=(address%4)+n;
            s.mask |= 1U<<lane;
            s.data |= ((raw>>(8*n))&255U)<<(8*lane);
        }
        return s;
    }
    Input input(Store s) {
        Input i; i.desc=s; i.head_id=i.admit_id=s.id; i.response_id=transaction; return i;
    }
    Input noise() {
        Input i;
        i.desc={unsigned(rng()%8192),unsigned(rng()%4),unsigned(rng()%16),uint32_t(rng()),uint32_t(rng())};
        i.head_id=rng()%8192; i.admit_id=rng()%8192;
        i.candidate=rng()%2; i.store=rng()%2; i.fault=rng()%2; i.uncached=rng()%2;
        i.recovery=rng()%2; i.head=rng()%2; i.admit=rng()%2; i.response_id=transaction;
        return i;
    }
    void step(Input i) {
        d.clk_i=0; d.rst_i=i.reset; d.recovery_i=i.recovery;
        d.desc_valid_i=i.candidate; d.store_i=i.store; d.fault_i=i.fault; d.uncached_i=i.uncached;
        d.id_i=i.desc.id; d.address_i=i.desc.address; d.size_i=i.desc.size;
        d.write_data_i=i.desc.data; d.byte_mask_i=i.desc.mask;
        d.head_valid_i=i.head; d.head_id_i=i.head_id; d.admit_valid_i=i.admit; d.admit_id_i=i.admit_id;
        d.commit_i=i.commit; d.request_ready_i=i.request_ready; d.response_valid_i=i.response;
        for (unsigned n=0;n<(RESPONSE_BITS+31)/32;++n) d.response_i[n]=0;
        put(d.response_i,RESPONSE_TRANSACTION_ID_OFFSET,4,i.response_id);
        put(d.response_i,RESPONSE_STATUS_OFFSET,2,i.status);
        if (i.payload) put(d.response_i,(i.payload==1 ? RESPONSE_LINE_READ_DATA_OFFSET : RESPONSE_UNCACHED_READ_DATA_OFFSET)+i.payload_bit,1,1);
        d.eval();
        bool live=!i.reset && !fatal;
        bool ready=live && !owned && !i.recovery && !i.response && i.candidate && i.store
            && !i.fault && !i.uncached && i.head && i.admit && i.head_id==i.desc.id && i.admit_id==i.desc.id;
        bool capture=ready && i.commit, offer=live && owned && !accepted;
        require(bool(d.commit_ready_o)==ready,"retirement eligibility");
        require(bool(d.accept_o)==capture,"atomic acceptance");
        require(bool(d.request_valid_o)==offer,"offered write ownership");
        require(bool(d.response_ready_o)==(live && owned && accepted),"response ownership");
        require(bool(d.busy_o)==(!i.reset && owned),"committed ownership retention");
        if (!i.reset) require(bool(d.fatal_o)==bool(fatal) && d.fatal_reason_o==fatal,"first fatal reason");
        bool visible=!i.reset && owned;
        require(d.owner_id_o==(visible ? held.id:0),"full ROB identity");
        require(d.line_address_o==(visible ? held.address&~31U:0),"owned line address");
        std::array<uint32_t,8> bytes{};
        uint32_t mask=0;
        if (visible) for (unsigned lane=0;lane<4;++lane) if ((held.mask>>lane)&1U) {
            unsigned offset=(held.address%32)-(held.address%4)+lane;
            mask |= 1U<<offset;
            bytes.at(offset/4) |= ((held.data>>(8*lane))&255U)<<(8*(offset%4));
        }
        require(d.line_mask_o==mask,"retained byte mask");
        for (unsigned n=0;n<8;++n) require(d.line_data_o[n]==bytes[n],"retained store bytes");
        std::array<uint32_t,(REQUEST_BITS+31)/32> expected{};
        if (offer) {
            put(expected,REQUEST_TRANSACTION_ID_OFFSET,4,transaction);
            put(expected,REQUEST_WRITE_OFFSET,1,1);
            put(expected,REQUEST_ADDRESS_OFFSET,32,held.address&~31U);
            put(expected,REQUEST_LINE_WRITE_MASK_OFFSET,32,mask);
            for (unsigned n=0;n<8;++n) put(expected,REQUEST_LINE_WRITE_DATA_OFFSET+32*n,32,bytes[n]);
        }
        for (unsigned b=0;b<REQUEST_BITS;++b)
            require(packed_bits::bit(d.request_o,b)==packed_bits::bit(expected,b),"write packet/stability");
        unsigned error=0;
        if (i.response) error=!owned || !accepted ? 1 : i.response_id!=transaction ? 2
            : i.status>1 ? 3 : i.payload ? 4 : i.status==1 ? 5 : 0;
        d.clk_i=1; d.eval(); ++cycles;
        if (i.reset) { owned=accepted=false; fatal=transaction=0; ++count["reset"]; }
        else if (!fatal) {
            if (error) { fatal=error; ++count["fatal_"+std::to_string(error)]; }
            else {
                if (capture) { held=i.desc; owned=true; accepted=false; ++count["committed"]; }
                if (offer && i.request_ready) { accepted=true; ++count["requests"]; }
                if (i.response) {
                    owned=accepted=false; transaction=(transaction+1)%16; ++count["responses"];
                    if (!transaction) ++count["id_wrap"];
                }
            }
        }
    }
    void reset() { Input i; i.reset=true; step(i); step(noise()); }
    void qualifiers(Store s) {
        auto base=input(s);
        for (unsigned k=0;k<8;++k) {
            auto i=base;
            if (k==0) i.candidate=false;
            if (k==1) i.store=false;
            if (k==2) i.fault=true;
            if (k==3) i.uncached=true;
            if (k==4) i.head=false;
            if (k==5) i.admit=false;
            if (k==6) i.recovery=true;
            if (k==7) i.reset=true;
            step(i); ++count["blocked_"+std::to_string(k)];
        }
        for (unsigned bit=0;bit<13;++bit) {
            auto i=base; i.head_id^=1U<<bit; step(i); ++count["head_bit_"+std::to_string(bit)];
            i=base; i.admit_id^=1U<<bit; step(i); ++count["admit_bit_"+std::to_string(bit)];
            i=base; i.desc.id^=1U<<bit; step(i); ++count["desc_bit_"+std::to_string(bit)];
        }
        for (unsigned n=0;n<5;++n) { step(base); ++count["ready_without_commit"]; }
    }
    void commit(Store s) { auto i=input(s); i.commit=true; step(i); ++cases; }
    void run(Store s, bool fast, bool permission) {
        commit(s);
        unsigned stalls=fast ? 0:1+rng()%5;
        auto controls=[&]() {
            auto i=permission ? input(s):noise();
            i.recovery=!permission; i.head=i.admit=i.candidate=permission;
            return i;
        };
        for (unsigned n=0;n<stalls;++n) { step(controls()); ++count["request_stall"]; }
        auto i=controls(); i.request_ready=true; step(i);
        ++count[fast ? "immediate_request":"delayed_request"];
        for (unsigned n=0;n<stalls;++n) { step(controls()); ++count["response_stall"]; }
        i=controls(); i.response=true; i.response_id=transaction; step(i);
        ++count[fast ? "immediate_response":"delayed_response"];
        ++count[permission ? "permission_retained":"permission_withdrawn"];
        ++count["size_"+std::to_string(s.size)]; ++count["offset_"+std::to_string(s.address%32)];
        step(noise());
    }
    void protocol(unsigned kind, unsigned bit=0) {
        reset(); Store s=make(8191,0,31); auto i=input(s);
        if (kind!=0) commit(s);
        if (kind>=3) { i.request_ready=true; step(i); }
        if (kind==10) { i=input(s); i.response=true; step(i); }
        i=input(s); i.response=true;
        if (kind==2) i.request_ready=true;
        if (kind==3) { i.response_id^=1U<<bit; response_id_bits.set(bit); }
        if (kind==4) i.status=1;
        if (kind==5) i.status=2;
        if (kind==6) i.status=3;
        if (kind==7) { i.payload=1; i.payload_bit=bit; line_payload_bits.set(bit); }
        if (kind==8) { i.payload=2; i.payload_bit=bit; word_payload_bits.set(bit); }
        if (kind==9) { i.payload=1; i.status=1; }
        step(i); require(fatal!=0,"negative response must be fatal");
        if (kind==0 || kind==10) { step(input(s)); ++count["fatal_blocks_commit"]; }
        for (unsigned n=0;n<3;++n) { auto j=noise(); j.request_ready=j.response=true; j.status=3; step(j); ++count["fatal_hold"]; }
        ++count["bad_"+std::to_string(kind)]; reset();
    }
    void reset_phases() {
        for (unsigned phase=0;phase<3;++phase) {
            reset(); auto s=make(33,2,28);
            if (phase) commit(s);
            if (phase==2) { auto i=input(s); i.request_ready=true; step(i); }
            reset(); ++count["reset_phase_"+std::to_string(phase)];
        }
    }
    void negative(const std::string& name) {
        reset(); auto s=make(7,2,28); auto i=input(s); i.commit=true;
        if (name=="no-admission") i.admit=false;
        else if (name=="recovery") i.recovery=true;
        else if (name=="busy") commit(s);
        else if (name=="size") i.desc.size=3;
        else if (name=="alignment") i.desc.address=29;
        else if (name=="mask") i.desc.mask=3;
        else if (name=="data") { i.desc=make(7,0,28); i.desc.data|=0x100; }
        else throw std::runtime_error("unknown negative");
        d.clk_i=0; d.rst_i=0; d.recovery_i=i.recovery;
        d.desc_valid_i=d.store_i=d.head_valid_i=1; d.fault_i=d.uncached_i=0;
        d.admit_valid_i=i.admit; d.id_i=d.head_id_i=d.admit_id_i=7;
        d.address_i=i.desc.address; d.write_data_i=i.desc.data; d.byte_mask_i=i.desc.mask; d.size_i=i.desc.size;
        d.commit_i=1; d.request_ready_i=d.response_valid_i=0; d.eval(); d.clk_i=1; d.eval();
        throw std::runtime_error("caller violation survived");
    }
};
int main(int argc,char** argv) {
    Verilated::commandArgs(argc,argv);
    try {
        if (argc==3 && std::string(argv[1])=="negative") { Bench b(1); b.negative(argv[2]); return 1; }
        unsigned seed=argc>1 ? std::stoul(argv[1]):1, random=argc>2 ? std::stoul(argv[2]):2000;
        Bench b(seed); b.reset(); b.qualifiers(b.make(8191,2,0xfffffffc));
        for (unsigned size=0;size<3;++size) for (unsigned offset=0;offset<32;offset+=1U<<size)
            for (unsigned timing=0;timing<4;++timing) b.run(b.make(8191-offset,size,0x1000+offset),timing&1,timing&2);
        for (unsigned id=0;id<8192;++id) b.run(b.make(id,0,id),true,id&1);
        b.count["all_identities"]=8192;
        b.run(b.make(8191,2,0xfffffffc),false,false); ++b.count["address_end"];
        for (unsigned kind=0;kind<11;++kind) b.protocol(kind);
        for (unsigned bit=0;bit<4;++bit) b.protocol(3,bit);
        for (unsigned bit=0;bit<256;++bit) b.protocol(7,bit);
        for (unsigned bit=0;bit<32;++bit) b.protocol(8,bit);
        b.reset_phases();
        for (unsigned n=0;n<random;++n) {
            unsigned size=b.rng()%3; uint32_t address=b.rng() & ~((1U<<size)-1);
            b.run(b.make(b.rng()%8192,size,address),b.rng()%2,b.rng()%2); ++b.count["random"];
        }
        b.require(!b.owned && !b.fatal,"final drain");
        b.count["response_id_bits"]=b.response_id_bits.count();
        b.count["line_payload_bits"]=b.line_payload_bits.count();
        b.count["word_payload_bits"]=b.word_payload_bits.count();
        std::cout<<"COMMITTED STORE PASS seed="<<seed<<" cycles="<<b.cycles<<" cases="<<b.cases<<" random="<<random;
        b.count.erase("random"); for (const auto& c:b.count) std::cout<<" "<<c.first<<"="<<c.second;
        std::cout<<"\n";
    } catch (const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
