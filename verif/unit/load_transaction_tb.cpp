#include "Vload_transaction.h"
#include "verilated.h"
#include "packed_bits.hpp"
#include "fetch_memory_layout.hpp"
#include <array>
#include <cstdint>
#include <iostream>
#include <map>
#include <random>
#include <stdexcept>
#include <string>
using packed_bits::put;

struct Load {
    unsigned id=0, size=0;
    uint32_t address=0, cause=0;
    bool unsign=false, uncached=false, head=false, fault=false;
};
struct Input {
    bool reset=false, launch=false, kill=false, take=false, permit=false, head=false;
    bool request_ready=false, response=false;
    unsigned head_id=0, status=0, corrupt=0;
    Load load;
};
enum Phase {IDLE, WAIT, OFFER, PENDING, OUTPUT, FATAL};
struct Timing { bool fast=false; unsigned controls=0; };
struct Bench {
    Vload_transaction d;
    std::mt19937 rng;
    unsigned cycles=0, cases=0, transaction=0;
    std::map<std::string, unsigned> count;
    std::array<uint8_t,32> bytes{};
    explicit Bench(unsigned seed): rng(seed) {}
    void require(bool good, const std::string& text) const {
        if (!good) throw std::runtime_error("load transaction mismatch cycle="+std::to_string(cycles)+": "+text);
    }
    uint32_t word(unsigned start) const {
        uint32_t result=0;
        for (unsigned b=0;b<4;++b) result |= uint32_t(bytes.at(start+b))<<(8*b);
        return result;
    }
    uint32_t value(const Load& l) const {
        uint32_t result=0;
        unsigned width=1U<<l.size;
        for (unsigned b=0;b<width;++b) result |= uint32_t(bytes.at((l.address%32)+b))<<(8*b);
        if (!l.unsign && width<4 && (bytes.at(l.address%32+width-1)&128))
            result |= ~((1U<<(8*width))-1);
        return result;
    }
    void step(Phase phase, const Load& held, Input i={}, unsigned response_status=0) {
        d.clk_i=0; d.rst_i=i.reset; d.launch_i=i.launch; d.kill_i=i.kill; d.take_i=i.take;
        d.id_i=i.load.id; d.address_i=i.load.address; d.size_i=i.load.size;
        d.unsigned_i=i.load.unsign; d.uncached_i=i.load.uncached; d.head_only_i=i.load.head;
        d.fault_i=i.load.fault; d.cause_i=i.load.cause; d.trap_value_i=i.load.address;
        d.issue_allowed_i=i.permit; d.head_valid_i=i.head; d.head_id_i=i.head_id;
        d.request_ready_i=i.request_ready; d.response_valid_i=i.response;
        for (unsigned n=0;n<(RESPONSE_BITS+31)/32;++n) d.response_i[n]=0;
        put(d.response_i,RESPONSE_TRANSACTION_ID_OFFSET,4,transaction^(i.corrupt==1 ? 1U:0U));
        put(d.response_i,RESPONSE_STATUS_OFFSET,2,i.status);
        if (i.response && !held.fault && (i.status==0 || i.corrupt==3)) {
            if (held.uncached) {
                uint32_t lanes=0;
                for (unsigned b=0;b<(1U<<held.size);++b)
                    lanes |= uint32_t(bytes.at(held.address%32+b))<<(8*(held.address%4+b));
                put(d.response_i,RESPONSE_UNCACHED_READ_DATA_OFFSET,32,lanes);
            }
            else for (unsigned n=0;n<8;++n) put(d.response_i,RESPONSE_LINE_READ_DATA_OFFSET+32*n,32,word(4*n));
        }
        if (i.corrupt==2) put(d.response_i,held.uncached ? RESPONSE_LINE_READ_DATA_OFFSET : RESPONSE_UNCACHED_READ_DATA_OFFSET,32,1);
        if (i.corrupt==4) put(d.response_i,RESPONSE_UNCACHED_READ_DATA_OFFSET,8,0x5a);
        d.eval();
        bool active=!i.reset && phase!=FATAL;
        bool valid=active && phase==OUTPUT && !i.kill;
        bool busy=active && phase!=IDLE;
        require(bool(d.ready_o)==(active && phase==IDLE && !i.kill),"launch capacity");
        require(bool(d.busy_o)==busy && d.owner_id_o==(busy ? held.id:0),"owner identity/drain");
        require(bool(d.request_valid_o)==(active && phase==OFFER),"request validity");
        require(bool(d.response_ready_o)==(active && phase==PENDING),"response ownership");
        require(bool(d.valid_o)==valid,"completion validity");
        if (!i.reset) require(bool(d.fatal_o)==(phase==FATAL),"fatal state");
        bool result_fault=held.fault || response_status==1;
        bool irreversible=busy && held.uncached && !held.fault
            && (phase==OFFER || phase==PENDING || (phase==OUTPUT && !result_fault));
        require(bool(d.irrevocable_o)==irreversible,"MMIO protection");
        std::array<uint32_t,(REQUEST_BITS+31)/32> expected{};
        if (active && phase==OFFER) {
            put(expected,REQUEST_TRANSACTION_ID_OFFSET,4,transaction);
            put(expected,REQUEST_UNCACHED_OFFSET,1,held.uncached);
            put(expected,REQUEST_ADDRESS_OFFSET,32,held.uncached ? held.address:held.address&~31U);
            if (held.uncached) put(expected,REQUEST_UNCACHED_SIZE_OFFSET,2,held.size);
        }
        for (unsigned b=0;b<REQUEST_BITS;++b)
            require(packed_bits::bit(d.request_o,b)==packed_bits::bit(expected,b),"request payload/stability/write exclusion");
        require(d.id_o==(valid ? held.id:0) && d.address_o==(valid ? held.address:0),"result identity/address");
        require(bool(d.fault_o)==(valid && result_fault),"fault indication");
        require(d.cause_o==(valid && result_fault ? (held.fault ? held.cause:5):0),"precise cause");
        require(d.trap_value_o==(valid && result_fault ? held.address:0),"precise trap value");
        require(d.data_o==(valid && !result_fault ? value(held):0),"load extraction/sign/data suppression");
        d.clk_i=1; d.eval(); ++cycles;
    }
    void reset(Phase phase=IDLE, Load l={}, unsigned status=0) {
        Input i; i.reset=true; step(phase,l,i,status); transaction=0;
        count["reset_"+std::to_string(phase)]++;
        step(IDLE,{});
    }
    Input noise() {
        Input i;
        i.load={unsigned(rng()%8192),unsigned(rng()%3),uint32_t(rng()),0,bool(rng()%2),bool(rng()%2),true,false};
        i.permit=rng()%2; i.head=rng()%2; i.head_id=rng()%8192;
        return i;
    }
    Input post_issue(const Load& l, unsigned controls) {
        Input i=noise();
        if (controls) {
            i.permit=i.head=controls==1;
            i.head_id=controls==1 ? l.id:l.id^32;
        }
        return i;
    }
    void run(Load l, unsigned cancel=0, unsigned status=0, int reset_phase=-1, Timing timing={}) {
        ++cases;
        Input i=noise(); i.launch=true; i.load=l;
        if (l.fault) {
            step(IDLE,{},i);
            count["local_fault_"+std::to_string(l.cause)]++;
            if (l.cause==4) ++count["misaligned_size_"+std::to_string(l.size)];
            if (l.address%32+(1U<<l.size)>32) ++count["fault_cross_line"];
            if (reset_phase==OUTPUT) {
                reset(OUTPUT,l); ++count["local_fault_reset"]; return;
            }
            i=noise(); step(OUTPUT,l,i);
            i.take=cancel!=6; i.kill=cancel==6; step(OUTPUT,l,i); step(IDLE,{});
            count[cancel==6 ? "local_fault_cancel":"local_fault_take"]++;
            return;
        }
        for (auto& b:bytes) b=uint8_t(rng());
        // Force both sign cases while retaining distinct surrounding bytes.
        bytes.at(l.address%32+(1U<<l.size)-1)=uint8_t((cases%2 ? 0x80:0x7f));
        step(IDLE,{},i);
        count["size_"+std::to_string(l.size)]++;
        count[l.uncached ? "uncached":"cached"]++;
        count[l.unsign ? "unsigned":"signed"]++;
        count["offset_"+std::to_string(l.address%32)]++;
        if (reset_phase==WAIT) { reset(WAIT,l); return; }
        i=noise(); i.permit=false; i.head=true; i.head_id=l.id;
        if (!timing.fast) {
            step(WAIT,l,i); ++count["hazard_stall"];
            i.permit=true;
            if (l.uncached || l.head) {
                i.head=false; step(WAIT,l,i); ++count["head_stall"];
                i.head=true; i.head_id=l.id^32; step(WAIT,l,i); ++count["head_generation"];
                i.head_id=l.id^1; step(WAIT,l,i);
            }
        }
        i.permit=true; i.head=l.head || l.uncached; i.head_id=i.head ? l.id:l.id^32;
        if (cancel==1) {
            i.kill=true; step(WAIT,l,i); step(IDLE,{}); ++count["kill_wait"]; return;
        }
        step(WAIT,l,i);
        if (!i.head) ++count["non_head_issue"];
        if (reset_phase==OFFER) { reset(OFFER,l); return; }
        unsigned request_wait=timing.fast ? unsigned(cancel==2):2+rng()%4;
        for (unsigned n=0;n<request_wait;++n) {
            i=post_issue(l,timing.controls); i.kill=cancel==2 && n==0;
            step(OFFER,l,i); ++count["request_stall"];
        }
        i=post_issue(l,timing.controls); i.request_ready=true; i.kill=cancel==3; step(OFFER,l,i);
        ++count[i.permit ? "request_permit_high":"request_permit_low"];
        ++count[i.head ? "request_head_high":"request_head_low"];
        if (!request_wait) ++count["request_first_cycle"];
        if (reset_phase==PENDING) { reset(PENDING,l); return; }
        unsigned response_wait=timing.fast ? unsigned(cancel==4):2+rng()%4;
        for (unsigned n=0;n<response_wait;++n) {
            i=post_issue(l,timing.controls); i.kill=cancel==4 && n==0;
            step(PENDING,l,i); ++count["response_wait"];
        }
        i=post_issue(l,timing.controls); i.response=true; i.status=status; i.kill=cancel==5; step(PENDING,l,i);
        if (!response_wait) ++count["response_first_cycle"];
        transaction=(transaction+1)%16; if (!transaction) ++count["id_wrap"];
        if (cancel>=2 && cancel<=5) {
            step(IDLE,{}); ++count["kill_"+std::to_string(cancel)];
            if (status) ++count["killed_fault"];
            return;
        }
        if (reset_phase==OUTPUT) { reset(OUTPUT,l,status); return; }
        unsigned result_wait=timing.fast ? 0:2+rng()%4;
        for (unsigned n=0;n<result_wait;++n) {
            i=post_issue(l,timing.controls); step(OUTPUT,l,i,status); ++count["result_stall"];
        }
        i=post_issue(l,timing.controls); i.take=cancel!=6; i.kill=cancel==6; step(OUTPUT,l,i,status);
        count[cancel==6 ? "kill_result":"completed"]++;
        if (status) ++count["response_fault"];
        if (!result_wait && i.take) ++count["result_first_cycle"];
        if (timing.fast && !cancel) ++count[l.uncached ? "fast_uncached":"fast_cached"];
        if (!cancel && timing.controls==1) ++count["permission_retained"];
        if (!cancel && timing.controls==2) ++count["permission_dropped"];
        step(IDLE,{});
    }
    void bad_response(unsigned kind, bool uncached, bool stale=false) {
        reset();
        Load l{8191,0,uncached ? 0x10000003U:0x43U,0,false,uncached,true,false};
        for (auto& b:bytes) b=0xa5;
        Input i; Phase phase=IDLE;
        if (kind!=0) {
            i.launch=true; i.load=l; step(IDLE,{},i);
            i={}; i.permit=kind!=9; i.head=true; i.head_id=l.id; step(WAIT,l,i);
            phase=kind==9 ? WAIT:OFFER;
            if (kind!=1 && kind!=9) {
                i={}; i.request_ready=true; i.kill=stale; step(OFFER,l,i); phase=PENDING;
                if (kind==7) {
                    i={}; i.response=true; step(PENDING,l,i); transaction=(transaction+1)%16; phase=OUTPUT;
                }
            }
        }
        i={}; i.response=true; i.request_ready=true;
        if (kind==8) i.corrupt=4;
        if (kind==2) i.corrupt=1;
        if (kind==3) i.status=2;
        if (kind==4) i.status=3;
        if (kind==5) i.corrupt=2;
        if (kind==6) { i.status=1; i.corrupt=3; }
        step(phase,l,i);
        for (unsigned n=0;n<4;++n) { i={}; i.kill=true; i.permit=true; step(FATAL,l,i); }
        ++count["bad_"+std::to_string(kind)];
        if (stale) ++count["stale_malformed"];
        reset(FATAL,l);
    }
    void negative(const std::string& name) {
        reset(); Input i; Load l{5,0,0,0,false,true,true,false};
        if (name=="capacity" || name=="mmio-kill") {
            i.launch=true; i.load=l; step(IDLE,{},i);
            if (name=="capacity") { d.launch_i=1; d.clk_i=0; d.eval(); d.clk_i=1; d.eval(); }
            else {
                i={}; i.permit=true; i.head=true; i.head_id=l.id; step(WAIT,l,i);
                d.kill_i=1; d.clk_i=0; d.eval(); d.clk_i=1; d.eval();
            }
        } else {
            d.clk_i=0;
            if (name=="take") d.take_i=1;
            else {
                d.launch_i=1;
                if (name=="size") d.size_i=3;
                if (name=="unsigned-word") { d.size_i=2; d.unsigned_i=1; }
                if (name=="alignment") { d.size_i=2; d.address_i=1; }
                if (name=="fault") { d.fault_i=1; d.cause_i=7; }
            }
            d.eval(); d.clk_i=1; d.eval();
        }
        throw std::runtime_error("negative assertion did not fire");
    }
};
int main(int argc,char** argv) {
    Verilated::commandArgs(argc,argv);
    try {
        if (argc>1 && std::string(argv[1])=="negative") { Bench b(1); b.negative(argv[2]); return 1; }
        unsigned seed=argc>1 ? std::stoul(argv[1]):1;
        unsigned random=argc>2 ? std::stoul(argv[2]):2000;
        Bench b(seed); b.reset();
        for (unsigned id=0;id<8192;++id) {
            Load l{id,0,0x20U+id%32,0,bool(id%2),false,false,false}; b.run(l);
        }
        ++b.count["all_identities"];
        for (bool mmio:{false,true}) for (unsigned size=0;size<3;++size)
            for (unsigned offset=0;offset<32;offset+=(1U<<size)) for (bool unsign:{false,true}) {
                if (size==2 && unsign) continue;
                Load l{0x1fa5,size,(mmio ? 0x10001000U:0xffe0U)+offset,0,unsign,mmio,false,false};
                b.run(l); b.run(l,0,1);
                if (!mmio) for (unsigned kill=1;kill<=6;++kill) { b.run(l,kill); b.run(l,kill,1); }
                else b.run(l,1);
            }
        for (unsigned size=0;size<3;++size)
            for (uint32_t address:{0x10000U,0x10001U,0x1001dU,0x1001eU,0x1001fU,
                                  0xfffffffcU,0xfffffffdU,0xfffffffeU,0xffffffffU}) {
                unsigned cause=(address&((1U<<size)-1)) ? 4:5;
                Load l{8190,size,address,cause,false,false,true,true};
                b.run(l); b.run(l,6); b.run(l,0,0,OUTPUT);
                l.uncached=true; b.run(l);
            }
        for (bool mmio:{false,true}) for (unsigned controls:{1,2})
            for (unsigned size=0;size<3;++size) for (unsigned offset=0;offset<32;offset+=(1U<<size)) {
                Load l{123,size,(mmio ? 0x10001000U:0x80U)+offset,0,false,mmio,mmio,false};
                b.run(l,0,0,-1,{true,controls}); b.run(l,0,1,-1,{true,controls});
                b.run(l,0,0,-1,{false,controls});
            }
        for (int phase=WAIT;phase<=OUTPUT;++phase) for (bool mmio:{false,true}) {
            Load l{31,1,mmio ? 0x10000002U:0x1232U,0,false,mmio,true,false}; b.run(l,0,0,phase);
        }
        for (unsigned kind=0;kind<10;++kind) for (bool mmio:{false,true}) {
            if (kind==8 && !mmio) continue;
            b.bad_response(kind,mmio);
            if (!mmio && kind>=2 && kind<=6) b.bad_response(kind,false,true);
        }
        for (unsigned n=0;n<random;++n) {
            unsigned size=b.rng()%3;
            Load l{unsigned(b.rng()%8192),size,uint32_t(b.rng()%65536)&~((1U<<size)-1),0,
                size<2 && bool(b.rng()%2),bool(b.rng()%3==0),bool(b.rng()%2),false};
            if (l.uncached) l.address=0x10001000U+l.address%32;
            unsigned kill=l.uncached ? b.rng()%2:b.rng()%7;
            b.run(l,kill,unsigned(b.rng()%5==0),-1,{bool(b.rng()%2),0});
        }
        std::cout<<"LOAD TRANSACTION PASS seed="<<seed<<" cycles="<<b.cycles<<" cases="<<b.cases<<" random="<<random;
        for (const auto& [key,value]:b.count) std::cout<<" "<<key<<"="<<value;
        std::cout<<"\n";
    } catch (const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
