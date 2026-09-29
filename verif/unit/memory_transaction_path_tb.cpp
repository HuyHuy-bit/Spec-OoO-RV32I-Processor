#include "Vmemory_transaction_path.h"
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

struct Desc {
    unsigned id=0, kind=0, size=0, cause=0;
    uint32_t address=0, raw=0;
    bool unsign=false;
    bool store() const { return kind>=2; }
    bool uncached() const { return kind==1 || kind==3; }
    unsigned client() const { return !store() ? 0 : (uncached() || cause) ? 2:1; }
    unsigned mask() const { return cause ? 0:((1U<<(1U<<size))-1)<<(address%4); }
    uint32_t bytes() const {
        uint32_t bits=size==0 ? 255U:size==1 ? 65535U:0xffffffffU;
        return store() && !cause ? (raw&bits)<<(8*(address%4)):0;
    }
};
struct Bench {
    Vmemory_transaction_path d;
    std::mt19937 rng;
    Desc held;
    std::array<unsigned,3> ids{};
    std::array<uint8_t,256> memory{};
    bool owned=false, committed=false, offered=false, sent=false, authorized=false;
    bool killed=false, result=false, fatal_expected=false;
    unsigned expected_cause=0, cycles=0, cases=0;
    uint32_t expected_data=0;
    std::map<std::string,unsigned> count;
    explicit Bench(unsigned seed):rng(seed) {
        for (auto& b:memory) b=uint8_t(rng());
        controls(); reset();
    }
    void require(bool ok,const std::string& why) const {
        if (!ok) throw std::runtime_error("memory path mismatch cycle="+std::to_string(cycles)+": "+why);
    }
    void controls() {
        d.clk_i=0; d.rst_i=0; d.recovery_i=0; d.kill_i=0; d.desc_valid_i=0;
        d.commit_i=0; d.take_i=0; d.head_valid_i=0; d.issue_allowed_i=0;
        d.admit_valid_i=0; d.request_ready_i=0; d.response_valid_i=0;
        for (unsigned n=0;n<(RESPONSE_BITS+31)/32;++n) d.response_i[n]=0;
    }
    void descriptor(Desc s) {
        d.id_i=s.id; d.address_i=s.address; d.size_i=s.size; d.store_i=s.store();
        d.unsigned_i=s.unsign; d.uncached_i=s.uncached() && !s.cause;
        d.head_only_i=s.store() || s.uncached() || s.cause;
        d.fault_i=bool(s.cause); d.cause_i=s.cause; d.trap_value_i=s.cause ? s.address:0;
        d.byte_mask_i=s.mask(); d.write_data_i=s.bytes();
    }
    Desc make(unsigned kind,unsigned size=2,unsigned cause=0) {
        Desc s; s.id=rng()%8192; s.kind=kind; s.size=size; s.cause=cause;
        s.address=(0x1000U+(rng()%256))&~((1U<<size)-1);
        s.raw=rng(); s.unsign=!s.store() && size<2 && rng()%2;
        return s;
    }
    void head(bool enable=true) { d.head_valid_i=enable; d.head_id_i=held.id; }
    void idle_inputs() { controls(); descriptor(make(rng()%4,rng()%3)); }
    void tick() {
        d.clk_i=0; d.eval();
        if (d.rst_i) {
            require(!d.desc_take_o && !d.commit_ready_o && !d.commit_accept_o && !d.valid_o
                && !d.request_valid_o && !d.response_ready_o && !d.busy_o && !d.irrevocable_o,"reset channels");
        } else {
            if (d.desc_take_o && !d.store_i) require(d.size_i<=2 && !(d.size_i==2 && d.unsigned_i),"stimulus load size");
            require(!d.commit_accept_o || (d.commit_ready_o && d.commit_i && d.desc_take_o),"atomic store consumption");
            if (fatal_expected) require(d.fatal_o && d.busy_o && !d.valid_o && !d.commit_ready_o
                && !d.desc_take_o && !d.request_valid_o && !d.response_ready_o,"fatal stop");
            else require(!d.fatal_o,"unexpected fatal");
            if (owned && !fatal_expected) {
                require(d.busy_o && d.owner_valid_o && d.owner_id_o==held.id,"retained owner");
                require(!d.desc_take_o && !d.commit_ready_o,"single-owner backpressure");
                require(bool(d.committed_o)==committed,"committed ownership");
                if (offered && held.uncached()) require(d.irrevocable_o,"device ownership through result");
            }
            std::array<uint32_t,(REQUEST_BITS+31)/32> packet{};
            if (d.request_valid_o) {
                require(owned && !sent && !held.cause && (authorized || committed),"request authorization or uniqueness");
                put(packet,REQUEST_TRANSACTION_ID_OFFSET,4,ids[held.client()]);
                put(packet,REQUEST_WRITE_OFFSET,1,held.store());
                put(packet,REQUEST_UNCACHED_OFFSET,1,held.uncached());
                put(packet,REQUEST_ADDRESS_OFFSET,32,held.uncached() ? held.address:held.address&~31U);
                if (held.uncached()) {
                    put(packet,REQUEST_UNCACHED_SIZE_OFFSET,2,held.size);
                    put(packet,REQUEST_UNCACHED_WRITE_DATA_OFFSET,32,held.bytes());
                    put(packet,REQUEST_UNCACHED_WRITE_STROBE_OFFSET,4,held.store() ? held.mask():0);
                } else if (held.store()) {
                    put(packet,REQUEST_LINE_WRITE_DATA_OFFSET+32*((held.address%32)/4),32,held.bytes());
                    put(packet,REQUEST_LINE_WRITE_MASK_OFFSET,32,held.mask()<<(4*((held.address%32)/4)));
                }
                offered=true; ++count["offers_"+std::to_string(held.kind)];
                if (!d.request_ready_i) ++count["request_stall"];
            } else if (offered && !sent && !fatal_expected) require(false,"withdrawn offer");
            for (unsigned n=0;n<packet.size();++n) require(d.request_o[n]==packet[n],"request packet");
            if (d.valid_o) {
                require(owned && result && !killed && !d.response_valid_i && d.head_valid_i
                    && d.head_id_i==held.id,"result eligibility");
                require(d.id_o==held.id && d.address_o==held.address && d.size_o==held.size
                    && bool(d.store_o)==held.store(),"result descriptor");
                require(bool(d.fault_o)==bool(expected_cause) && d.cause_o==expected_cause
                    && d.trap_value_o==(expected_cause ? held.address:0),"result fault");
                require(d.data_o==expected_data && d.write_data_o==(expected_cause ? 0:held.bytes())
                    && d.byte_mask_o==(expected_cause ? 0:held.mask()),"result data and bytes");
            } else require(!d.id_o && !d.address_o && !d.data_o && !d.write_data_o && !d.byte_mask_o
                && !d.size_o && !d.store_o && !d.fault_o && !d.cause_o && !d.trap_value_o,"inactive result zero");
            if (owned && !committed && !killed && !d.kill_i && !d.recovery_i && d.head_valid_i
                && d.head_id_i==held.id && d.issue_allowed_i) authorized=true;
            if (d.request_valid_o && d.request_ready_i) { sent=true; ++count["requests"]; }
            if (d.response_valid_i && d.response_ready_o) ++count["responses"];
        }
        Verilated::timeInc(1); d.clk_i=1; d.eval(); d.clk_i=0; Verilated::timeInc(1); ++cycles;
    }
    void reset(unsigned collision=0) {
        controls(); d.rst_i=1;
        d.head_valid_i=1; d.head_id_i=held.id;
        if (collision==1) { d.desc_valid_i=1; d.issue_allowed_i=1; }
        if (collision==2) { d.desc_valid_i=1; d.admit_valid_i=1; d.admit_id_i=held.id; d.commit_i=1; }
        if (collision==3) d.take_i=1;
        if (collision==4) d.response_valid_i=1;
        if (collision==5) d.request_ready_i=1;
        tick(); controls(); ids.fill(0);
        owned=committed=offered=sent=authorized=killed=result=fatal_expected=false;
        expected_cause=0; expected_data=0; ++count["reset"];
        tick(); require(!d.busy_o && !d.fatal_o,"reset release");
    }
    void idle() {
        controls(); tick(); require(!d.busy_o,"drained");
    }
    void start(Desc s) {
        held=s; idle_inputs(); descriptor(s); d.desc_valid_i=1; d.eval();
        require(!d.busy_o,"start capacity");
        d.recovery_i=1; tick(); require(!d.desc_take_o,"recovery blocks descriptor");
        d.recovery_i=0; d.eval();
        if (s.client()==1) {
            require(!d.desc_take_o && !d.request_valid_o,"store held before commit");
            head(); d.admit_valid_i=1; d.admit_id_i=s.id; d.eval();
            require(d.commit_ready_o && !d.desc_take_o,"store commit eligibility");
            for (unsigned bit=0;bit<13;++bit) {
                d.head_id_i=s.id^(1U<<bit); tick(); require(!d.commit_ready_o && !d.desc_take_o,"store full head identity");
                d.head_id_i=s.id; d.admit_id_i=s.id^(1U<<bit); tick(); require(!d.commit_ready_o,"store full admission identity");
            }
            d.admit_id_i=s.id; d.recovery_i=1; tick(); require(!d.commit_ready_o,"recovery beats commit");
            d.recovery_i=0; d.head_valid_i=0; tick(); require(!d.commit_ready_o,"store head valid");
            head(); d.admit_valid_i=0; tick(); require(!d.commit_ready_o,"store admission valid");
            d.admit_valid_i=1; d.commit_i=1; d.eval();
            require(d.commit_ready_o && d.commit_accept_o && d.desc_take_o,"store atomic acceptance");
            tick(); ++count["commits"]; committed=true;
        } else {
            require(d.desc_take_o,"load or device descriptor acceptance"); tick(); committed=false;
        }
        owned=true; offered=sent=authorized=killed=false;
        expected_cause=s.cause; expected_data=0; result=bool(s.cause);
        ++cases; ++count["class_"+std::to_string(s.kind)];
        idle_inputs();
        descriptor(make(0)); d.desc_valid_i=1;
        tick(); require(!d.desc_take_o,"next descriptor blocked");
        idle_inputs();
    }
    void blocked() {
        require(owned && !offered,"blocked setup");
        if (result || committed) return;
        for (unsigned bit=0;bit<13;++bit) {
            d.issue_allowed_i=1; head(); d.head_id_i=held.id^(1U<<bit);
            tick(); require(!d.request_valid_o,"full reservation head identity");
        }
        head(false); tick(); require(!d.request_valid_o,"reservation head valid");
        head(); d.issue_allowed_i=0; tick(); require(!d.request_valid_o,"reservation permission");
        d.issue_allowed_i=1; d.recovery_i=1; tick(); require(!d.request_valid_o,"reservation recovery");
        d.recovery_i=0; ++count["head_blocks"];
    }
    void offer(bool immediate=false) {
        head(); d.issue_allowed_i=1; d.request_ready_i=immediate;
        for (unsigned n=0;n<8;++n) {
            tick();
            if (immediate ? sent : bool(d.request_valid_o)) { if (immediate) ++count["immediate"]; return; }
        }
        require(false,"request watchdog");
    }
    void accept_request(unsigned stall=2,bool recover=false) {
        for (unsigned n=0;n<stall;++n) {
            idle_inputs(); d.recovery_i=recover; d.kill_i=recover && committed;
            tick(); require(d.request_valid_o,"held request");
        }
        d.request_ready_i=1; tick(); require(sent,"request acceptance"); idle_inputs();
    }
    void respond(unsigned status=0,unsigned bad=0) {
        for (unsigned n=0;n<(RESPONSE_BITS+31)/32;++n) d.response_i[n]=0;
        put(d.response_i,RESPONSE_TRANSACTION_ID_OFFSET,4,ids[held.client()]^(bad==1 ? 1:0));
        put(d.response_i,RESPONSE_STATUS_OFFSET,2,bad==2 ? 2:status);
        expected_cause=status ? (held.store() ? 7:5):0; expected_data=0;
        if (!held.store() && !status) {
            if (held.uncached()) {
                uint32_t word=0;
                for (unsigned b=0;b<(1U<<held.size);++b) word|=uint32_t(memory[(held.address+b)%256])<<(8*((held.address%4)+b));
                put(d.response_i,RESPONSE_UNCACHED_READ_DATA_OFFSET,32,word);
            } else for (unsigned w=0;w<8;++w) {
                uint32_t word=0;
                for (unsigned b=0;b<4;++b) word|=uint32_t(memory[((held.address&~31U)+4*w+b)%256])<<(8*b);
                put(d.response_i,RESPONSE_LINE_READ_DATA_OFFSET+32*w,32,word);
            }
            for (unsigned b=0;b<(1U<<held.size);++b) expected_data|=uint32_t(memory[(held.address+b)%256])<<(8*b);
            if (!held.unsign && held.size==0 && (expected_data&128)) expected_data|=0xffffff00U;
            if (!held.unsign && held.size==1 && (expected_data&32768)) expected_data|=0xffff0000U;
        }
        if (bad==3) put(d.response_i,held.uncached() ? RESPONSE_LINE_READ_DATA_OFFSET:RESPONSE_UNCACHED_READ_DATA_OFFSET,1,1);
        d.response_valid_i=1; d.eval(); require(d.response_ready_o,"response readiness"); tick();
        d.response_valid_i=0;
        if (bad || (committed && status)) { fatal_expected=true; return; }
        ids[held.client()]=(ids[held.client()]+1)%16;
        if (held.store() && !status) {
            for (unsigned b=0;b<(1U<<held.size);++b) memory[(held.address+b)%256]=uint8_t(held.raw>>(8*b));
            ++count["store_effects"];
        }
        if (committed || killed) { owned=false; result=false; committed=false; }
        else result=true;
    }
    void finish(unsigned stalls=2) {
        require(result,"finish result setup"); idle_inputs(); head(); d.issue_allowed_i=1;
        for (unsigned n=0;n<stalls;++n) { tick(); require(d.valid_o,"result held"); ++count["result_stall"]; }
        d.head_id_i=held.id^32; tick(); require(!d.valid_o,"result head identity");
        head(false); tick(); require(!d.valid_o,"result head valid"); head();
        d.take_i=1; d.recovery_i=1; tick(); owned=result=false; ++count["takes"]; idle();
    }
    void fatal_hold() {
        d.eval();
        unsigned owner=d.owner_id_o, valid=d.owner_valid_o, irrev=d.irrevocable_o, cs=d.committed_o;
        for (unsigned n=0;n<4;++n) {
            idle_inputs(); d.desc_valid_i=1; d.head_valid_i=1; d.head_id_i=owned ? held.id:d.id_i;
            d.admit_valid_i=1; d.admit_id_i=d.id_i; d.issue_allowed_i=1;
            d.request_ready_i=1; d.kill_i=!d.irrevocable_o; d.recovery_i=1;
            tick(); require(d.owner_id_o==owner && d.owner_valid_o==valid && d.irrevocable_o==irrev
                && d.committed_o==cs,"frozen fatal ownership"); ++count["fatal_hold"];
        }
    }
    void normal(Desc s,unsigned status=0,unsigned stalls=2) {
        start(s);
        if (!s.cause) {
            blocked(); offer(stalls==0);
            if (sent) idle_inputs(); else accept_request(stalls,committed);
            for (unsigned n=0;n<stalls;++n) {
                d.recovery_i=committed; d.kill_i=committed; tick(); ++count["response_wait"];
            }
            respond(status);
        } else ++count["local_fault"];
        if (owned) finish(stalls+1); else idle();
    }
    void cancel_case(unsigned phase) {
        auto s=make(0); start(s); blocked();
        if (phase>=1) offer();
        if (phase>=2) accept_request();
        if (phase==3) respond();
        if (phase==4) {
            d.kill_i=1; killed=true; respond(); idle(); ++count["kill_4"]; return;
        }
        idle_inputs(); head(); d.issue_allowed_i=1; d.kill_i=1; tick(); killed=true;
        if (phase==0 || phase==3) { owned=result=false; idle(); }
        else {
            for (unsigned n=0;n<3;++n) {
                descriptor(make(2)); d.desc_valid_i=1; d.admit_valid_i=1; d.admit_id_i=d.id_i;
                d.head_valid_i=1; d.head_id_i=d.id_i; tick(); require(!d.valid_o,"killed load result");
            }
            idle_inputs(); if (phase==1) accept_request();
            respond(); idle();
        }
        ++count["kill_"+std::to_string(phase)];
    }
    void unexpected(unsigned phase,unsigned kind) {
        reset();
        if (phase) {
            start(make(kind));
            if (phase>=2) offer();
            if (phase>=3) { accept_request(); respond(); head(); }
        } else {
            held=make(2); descriptor(held); d.desc_valid_i=1; head();
            d.admit_valid_i=1; d.admit_id_i=held.id;
        }
        if (phase==1) { head(); d.issue_allowed_i=1; }
        d.response_valid_i=1; d.eval();
        require(!d.valid_o && !d.commit_ready_o && !d.response_ready_o,"unexpected response blocks retirement");
        tick(); fatal_expected=true;
        if (phase==1) { require(!d.irrevocable_o,"unexpected response beats device grant"); ++count["grant_response_race"]; }
        fatal_hold();
        require(d.fatal_sources_o==8 && d.fatal_reason_o==64,"arbiter-only diagnostics");
        ++count["unexpected_"+std::to_string(phase)]; reset();
    }
    void directed() {
        for (unsigned kind=0;kind<4;++kind) for (unsigned size=0;size<3;++size) {
            normal(make(kind,size));
            if (kind!=2) normal(make(kind,size),1);
        }
        for (unsigned kind=0;kind<4;++kind) for (unsigned n=0;n<2;++n)
            normal(make(kind,1,(kind>=2 ? 6:4)+n));
        for (unsigned phase=0;phase<5;++phase) cancel_case(phase);
        for (unsigned kind=0;kind<4;++kind) for (unsigned fault=0;fault<2;++fault) {
            if (kind==2 && !fault) continue;
            start(make(kind,1,fault ? (kind>=2 ? 6:4):0));
            idle_inputs(); head(); d.issue_allowed_i=1; d.kill_i=1; tick();
            owned=result=false; idle(); ++count["preoffer_cancel"];
        }
        for (unsigned kind: {0U,1U,3U}) for (unsigned phase: {1U,2U,3U}) unexpected(phase,kind);
        unexpected(0,2);
        for (unsigned kind=0;kind<4;++kind) for (unsigned bad=1;bad<=3;++bad) {
            start(make(kind)); offer(); accept_request(); respond(0,bad); fatal_hold();
            require(d.fatal_sources_o==(8U|(1U<<held.client())),"engine fatal routing");
            ++count["malformed_"+std::to_string(kind)]; reset();
        }
        start(make(2)); offer(); accept_request(); respond(1); fatal_hold();
        require((d.fatal_reason_o&7)==5,"postcommit access fault fatal"); ++count["postcommit_fault"]; reset();
        for (unsigned kind=0;kind<4;++kind) for (unsigned phase=0;phase<4;++phase) {
            start(make(kind)); if (phase>=1) offer(); if (phase>=2) accept_request();
            if (phase==3) respond();
            reset(phase==0 ? 1:phase==1 ? 5:phase==2 ? 4:kind==2 ? 1:3); ++count["reset_phase_"+std::to_string(phase)];
        }
        held=make(2); descriptor(held); d.desc_valid_i=1; head(); d.admit_valid_i=1; d.admit_id_i=held.id;
        d.eval(); require(d.commit_ready_o,"reset commit setup"); reset(2); ++count["reset_commit"];
        held=make(0); descriptor(held); d.desc_valid_i=1; d.eval();
        require(d.desc_take_o,"reset descriptor setup"); reset(1); ++count["reset_descriptor"];
        for (unsigned n=0;n<20;++n) for (unsigned kind=0;kind<4;++kind) {
            auto s=make(kind,n%3); s.id=8191; normal(s,0,n%3); ++count["id_alias"];
        }
        for (unsigned size=0;size<3;++size) for (unsigned offset=0;offset<32;offset+=(1U<<size)) {
            auto s=make(2,size); s.address=0x1080+offset; normal(s);
            s.kind=0; s.unsign=size<2; normal(s); ++count["overlap"];
        }
    }
};
int main(int argc,char** argv) {
    Verilated::commandArgs(argc,argv);
    try {
        unsigned seed=argc>1 && std::string(argv[1])!="negative" ? std::stoul(argv[1]):1;
        Bench b(seed);
        if (argc>1 && std::string(argv[1])=="negative") {
            std::string name=argv[2];
            if (name=="take") { b.d.take_i=1; b.tick(); }
            else if (name=="commit") { b.d.commit_i=1; b.tick(); }
            else if (name=="kill-take") { b.d.kill_i=1; b.d.take_i=1; b.tick(); }
            else if (name=="load-mask" || name=="fault-mask") {
                b.held=b.make(0,1,name=="fault-mask" ? 4:0); b.descriptor(b.held);
                b.d.desc_valid_i=1; b.d.byte_mask_i^=1; b.tick();
            }
            else if (name=="mmio-kill" || name=="faulted-mmio-kill"
                || name=="mmio-store-kill" || name=="faulted-mmio-store-kill") {
                b.start(b.make(name.find("store")!=std::string::npos ? 3:1)); b.offer();
                if (name.find("faulted")!=std::string::npos) { b.accept_request(); b.respond(1); }
                b.idle_inputs(); b.d.kill_i=1; b.tick();
            } else throw std::runtime_error("unknown negative");
            throw std::runtime_error("negative survived");
        }
        unsigned random=argc>2 ? std::stoul(argv[2]):1000;
        b.directed();
        for (unsigned n=0;n<random;++n) b.normal(b.make(b.rng()%4,b.rng()%3),0,b.rng()%4);
        std::cout<<"MEMORY PATH PASS seed="<<seed<<" random="<<random<<" cases="<<b.cases<<" cycles="<<b.cycles;
        for (const auto& [key,value]:b.count) std::cout<<" "<<key<<"="<<value;
        std::cout<<"\n";
    } catch (const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
