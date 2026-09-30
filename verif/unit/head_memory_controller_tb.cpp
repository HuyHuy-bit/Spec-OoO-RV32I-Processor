#include "Vhead_memory_controller.h"
#include "verilated.h"
#include "packed_bits.hpp"
#include "backend_event_layout.hpp"
#include "fetch_memory_layout.hpp"
#include "platform_memory_layout.hpp"
#include <array>
#include <iostream>
#include <map>
#include <random>
#include <stdexcept>
#include <string>
using packed_bits::put;
using Event=std::array<uint32_t,(EVENT_BITS+31)/32>;
struct Operation {
    unsigned id=0,kind=0,rs1=0,rs2=0,rd=0,cause=0;
    uint32_t instruction=0,pc=0,source1=0,source2=0,address=0;
    bool store=false,uncached=false;
    unsigned size() const { return kind%4; }
    unsigned bytes() const { return 1U<<size(); }
    unsigned client() const { return !store ? 0:uncached ? 2:1; }
    unsigned mask() const { return ((1U<<bytes())-1)<<(address%4); }
    uint32_t write_data() const {
        uint32_t out=0;
        for (unsigned b=0;b<bytes();++b) out|=((source2>>(8*b))&255)<<(8*((address+b)%4));
        return out;
    }
};
struct Bench {
    Vhead_memory_controller d;
    std::mt19937 rng;
    Operation op;
    std::array<uint8_t,256> memory{};
    std::array<unsigned,3> ids{};
    bool owned=false,drain=false,committed=false,offered=false,sent=false,authorized=false;
    bool completed=false,fatal=false;
    unsigned cycles=0,cases=0,cause=0;
    std::map<std::string,unsigned> count;
    explicit Bench(unsigned seed):rng(seed) { for (auto& byte:memory) byte=uint8_t(rng()); reset(); }
    void require(bool ok,const std::string& why) const {
        if (!ok) throw std::runtime_error("head memory mismatch cycle="+std::to_string(cycles)+": "+why);
    }
    void controls() {
        d.clk_i=0; d.rst_i=0; d.recovery_i=0; d.cancel_i=0; d.memory_valid_i=0;
        d.memory_id_i=0; d.instruction_i=0; d.pc_i=0; d.source_ready_i=0; d.source1_i=0; d.source2_i=0;
        d.head_valid_i=0; d.head_id_i=0; d.head_pc_i=0; d.issue_allowed_i=0;
        d.admit_valid_i=0; d.admit_id_i=0; d.serial_accept_i=0; d.fault_accept_i=0; d.trap_accept_i=0;
        d.request_ready_i=0; d.response_valid_i=0;
        for (unsigned n=0;n<(RESPONSE_BITS+31)/32;++n) d.response_i[n]=0;
    }
    Operation make(bool store=false,unsigned kind=2,uint32_t address=64,int offset=-16,
                   unsigned rs1=3,unsigned rs2=4,unsigned rd=5) {
        Operation x; x.store=store; x.kind=kind; x.rs1=rs1; x.rs2=store ? rs2:0; x.rd=store ? 0:rd;
        x.id=rng()%8192; x.pc=rng()&~3U; x.source1=rs1 ? address-uint32_t(offset):0;
        x.source2=x.rs2 ? (x.rs2==rs1 ? x.source1:uint32_t(rng())):0; x.address=x.source1+uint32_t(offset);
        unsigned imm=unsigned(offset)&4095;
        x.instruction=store ? (imm>>5)<<25|rs2<<20|rs1<<15|kind<<12|(imm&31)<<7|0x23
                            : imm<<20|rs1<<15|kind<<12|rd<<7|3;
        bool permitted=false;
        for (const auto& r:REGIONS) if (x.address>=r.base
            && uint64_t(x.address)+x.bytes()<=uint64_t(r.base)+r.size && (store ? r.write:r.read)) {
            permitted=true; x.uncached=!r.cacheable;
        }
        if (x.address%x.bytes()) x.cause=store ? 6:4;
        else if (!permitted) x.cause=store ? 7:5;
        return x;
    }
    void descriptor(const Operation& x) {
        d.memory_valid_i=1; d.memory_id_i=x.id; d.instruction_i=x.instruction; d.pc_i=x.pc;
        d.source1_i=x.rs1 ? x.source1:0xdeadcafe; d.source2_i=x.rs2 ? x.source2:0xbaadfeed;
        d.source_ready_i=3;
    }
    void head() { d.head_valid_i=1; d.head_id_i=op.id; d.head_pc_i=op.pc; }
    bool live() const { return d.head_valid_i && d.head_id_i==op.id && d.head_pc_i==op.pc; }
    Event event() const {
        Event out{};
        put(out,VALID_OFFSET,1,1); put(out,PRIVILEGE_OFFSET,2,3);
        put(out,INSTRUCTION_OFFSET,32,op.instruction); put(out,PC_BEFORE_OFFSET,32,op.pc);
        put(out,PC_AFTER_OFFSET,32,cause ? op.pc:op.pc+4);
        put(out,RS1_ADDR_OFFSET,5,op.rs1); put(out,RS1_VALUE_OFFSET,32,op.source1);
        put(out,RS2_ADDR_OFFSET,5,op.rs2); put(out,RS2_VALUE_OFFSET,32,op.source2);
        if (cause) {
            put(out,TRAP_OFFSET,1,1); put(out,TRAP_CAUSE_OFFSET,32,cause); put(out,TRAP_VALUE_OFFSET,32,op.address);
        } else {
            put(out,RETIRED_OFFSET,1,1); put(out,MEM_VALID_OFFSET,1,1); put(out,MEM_ADDRESS_OFFSET,32,op.address);
            if (op.store) {
                put(out,MEM_WRITE_MASK_OFFSET,4,op.mask()); put(out,MEM_WRITE_DATA_OFFSET,32,op.write_data());
            } else {
                uint32_t value=0,lanes=0;
                for (unsigned b=0;b<op.bytes();++b) {
                    uint32_t byte=memory[(op.address+b)%256]; value|=byte<<(8*b); lanes|=byte<<(8*((op.address+b)%4));
                }
                if (op.kind==0 && (value&128)) value|=0xffffff00U;
                if (op.kind==1 && (value&32768)) value|=0xffff0000U;
                put(out,MEM_READ_MASK_OFFSET,4,op.mask()); put(out,MEM_READ_DATA_OFFSET,32,lanes);
                put(out,RD_ADDR_OFFSET,5,op.rd); put(out,RD_VALUE_OFFSET,32,op.rd ? value:0);
                put(out,RD_WRITE_MASK_OFFSET,32,op.rd ? UINT32_MAX:0);
            }
        }
        return out;
    }
    template<class T> void compare(const T& actual,const Event& expected) const {
        for (unsigned n=0;n<expected.size();++n) require(actual[n]==expected[n],"whole event word="+std::to_string(n));
    }
    void tick() {
        d.clk_i=0; d.eval();
        bool reading=!d.rst_i && !fatal && !owned && !drain && !d.recovery_i && !d.cancel_i
            && !d.response_valid_i && d.memory_valid_i && d.head_valid_i
            && d.memory_id_i==d.head_id_i && d.pc_i==d.head_pc_i;
        require(bool(d.read_request_o)==reading,"read ownership independent of values/readiness");
        require(bool(d.prepare_o)==(reading && d.source_ready_i==3),"both operands ready and single capture");
        if (d.rst_i) require(!d.busy_o && !d.serial_offer_o && !d.fault_offer_o && !d.trap_ready_o
            && !d.request_valid_o && !d.response_ready_o && !d.admission_valid_o,"reset visibility");
        else {
            require(bool(d.fatal_o)==fatal,"fatal status");
            if (fatal) require(d.busy_o && !d.serial_offer_o && !d.fault_offer_o && !d.trap_ready_o
                && !d.request_valid_o && !d.response_ready_o,"fatal freeze");
            if (owned || drain) require(d.busy_o,"retained busy");
            require(bool(d.committed_o)==committed,"committed drain visibility");
            require(bool(d.irrevocable_o)==(owned && op.uncached && !op.cause && (offered || d.request_valid_o)),
                "MMIO ownership through trap/retirement");
            bool admission=owned && op.store && !op.uncached && !op.cause && !fatal && !d.cancel_i;
            require(bool(d.admission_valid_o)==admission,"admission descriptor ownership");
            require(d.admission_id_o==(admission ? op.id:0) && d.admission_address_o==(admission ? op.address:0)
                && d.admission_data_o==(admission ? op.write_data():0) && d.admission_mask_o==(admission ? op.mask():0)
                && d.admission_size_o==(admission ? op.size():0),"admission metadata");
            if (d.serial_offer_o) require(owned && live() && !cause && !d.cancel_i && !d.response_valid_i,"serial eligibility");
            if (d.fault_offer_o) require(owned && live() && cause && !completed && !d.cancel_i && !d.response_valid_i,"fault once");
            if (d.trap_ready_o) require(owned && live() && cause && completed && !d.cancel_i && !d.response_valid_i,"trap after completion");
            if (admission && d.serial_offer_o) require(d.admit_valid_i && d.admit_id_i==op.id && !d.recovery_i,"store admission gating");
            compare(d.serial_event_o,d.serial_offer_o ? event():Event{});
            compare(d.fault_event_o,d.fault_offer_o ? event():Event{});
            require(d.serial_id_o==(d.serial_offer_o ? op.id:0) && d.fault_id_o==(d.fault_offer_o ? op.id:0),"event full identity");
            if (owned && live() && d.issue_allowed_i && !d.cancel_i && !d.recovery_i) authorized=true;
            std::array<uint32_t,(REQUEST_BITS+31)/32> packet{};
            if (d.request_valid_o) {
                require((owned || drain) && !sent && !op.cause && (authorized || committed),"bus authorization/uniqueness");
                require(!op.store || op.uncached || committed,"no cached write before retirement");
                put(packet,REQUEST_TRANSACTION_ID_OFFSET,4,ids[op.client()]); put(packet,REQUEST_WRITE_OFFSET,1,op.store);
                put(packet,REQUEST_UNCACHED_OFFSET,1,op.uncached); put(packet,REQUEST_ADDRESS_OFFSET,32,op.uncached ? op.address:op.address&~31U);
                if (op.uncached) {
                    put(packet,REQUEST_UNCACHED_SIZE_OFFSET,2,op.size());
                    put(packet,REQUEST_UNCACHED_WRITE_DATA_OFFSET,32,op.store ? op.write_data():0);
                    put(packet,REQUEST_UNCACHED_WRITE_STROBE_OFFSET,4,op.store ? op.mask():0);
                } else if (op.store) {
                    put(packet,REQUEST_LINE_WRITE_DATA_OFFSET+32*((op.address%32)/4),32,op.write_data());
                    put(packet,REQUEST_LINE_WRITE_MASK_OFFSET,32,op.mask()<<(op.address%32/4*4));
                }
                offered=true;
                if (!d.request_ready_i) ++count["request_stall"];
            } else if (offered && !sent && !fatal) require(false,"request withdrawn");
            for (unsigned n=0;n<packet.size();++n) require(packet[n]==d.request_o[n],"bus packet");
            if (d.request_valid_o && d.request_ready_i) { sent=true; ++count["requests"]; }
        }
        Verilated::timeInc(1); d.clk_i=1; d.eval(); d.clk_i=0; Verilated::timeInc(1); ++cycles;
    }
    void reset() {
        controls(); d.rst_i=1; tick(); controls(); owned=drain=committed=offered=sent=authorized=completed=fatal=false;
        cause=0; ids.fill(0); tick(); require(!d.busy_o,"reset release"); ++count["reset"];
    }
    void independence(bool recover=true) {
        d.eval(); Event serial{},fault_event{};
        for (unsigned n=0;n<serial.size();++n) { serial[n]=d.serial_event_o[n]; fault_event[n]=d.fault_event_o[n]; }
        unsigned offers=d.serial_offer_o|d.fault_offer_o<<1|d.trap_ready_o<<2;
        auto check=[&] { d.eval(); require((d.serial_offer_o|d.fault_offer_o<<1|d.trap_ready_o<<2)==offers,"combinational acceptance independence");
            compare(d.serial_event_o,serial); compare(d.fault_event_o,fault_event); };
        d.serial_accept_i=1; check(); d.serial_accept_i=0;
        d.fault_accept_i=1; check(); d.fault_accept_i=0;
        d.trap_accept_i=1; check(); d.trap_accept_i=0;
        if (recover) { d.recovery_i=1; check(); d.recovery_i=0; }
        d.memory_valid_i=0; check(); ++count["independence"];
    }
    void start(Operation x,bool checks=false) {
        op=x; cause=x.cause; controls(); descriptor(op); head();
        if (checks) {
            for (unsigned r=0;r<4;++r) { d.source_ready_i=r; d.eval(); require(d.read_request_o,"read independent of ready");
                if (r!=3) tick();
                ++count["ready_"+std::to_string(r)];
            }
            for (unsigned bit=0;bit<13;++bit) { d.head_id_i=op.id^(1U<<bit); tick(); }
            head(); d.head_pc_i^=4; tick(); head(); d.head_valid_i=0; tick(); head();
            d.recovery_i=1; tick(); d.recovery_i=0; d.cancel_i=1; tick(); d.cancel_i=0;
            ++count["prepare_blocked"];
        }
        d.source_ready_i=3; d.eval(); require(d.prepare_o,"prepare watchdog"); tick(); owned=true;
        completed=offered=sent=authorized=committed=false; ++cases;
        ++count[std::string(op.store ? "store_":"load_")+std::to_string(op.kind)];
        ++count["offset_"+std::to_string(op.address%4)];
        if (cause) ++count["cause_"+std::to_string(cause)];
        if (!op.rs1) ++count["x0_base"];
        if (op.store && !op.rs2) ++count["x0_store"];
        if (!op.store && !op.rd) ++count["x0_load"];
        controls(); head(); descriptor(op);
        d.instruction_i=~op.instruction; d.source1_i=~op.source1; d.source2_i=~op.source2; d.pc_i=op.pc; d.memory_id_i=op.id;
    }
    void block_owner() {
        for (unsigned bit=0;bit<13;++bit) { d.head_id_i=op.id^(1U<<bit); d.issue_allowed_i=1; tick();
            require(!d.request_valid_o && !d.serial_offer_o && !d.fault_offer_o && !d.trap_ready_o,"held owner identity"); }
        head(); d.head_pc_i^=4; tick(); require(!d.request_valid_o && !d.serial_offer_o && !d.fault_offer_o,"held owner PC");
        head(); d.head_valid_i=0; tick(); head(); d.issue_allowed_i=0; ++count["owner_blocked"];
    }
    void await_result() {
        for (unsigned n=0;n<8;++n) { d.eval(); if (d.serial_offer_o || d.fault_offer_o || d.trap_ready_o) return; tick(); }
        require(false,"result watchdog");
    }
    void accept_serial() {
        d.eval(); require(d.serial_offer_o,"serial acceptance setup");
        d.serial_accept_i=1; tick(); d.serial_accept_i=0; owned=false;
        if (op.store && !op.uncached) { committed=drain=true; ++count["commits"]; }
        ++count["retired"];
    }
    void offer() {
        if (!committed) head();
        d.issue_allowed_i=1;
        for (unsigned n=0;n<8;++n) { tick(); if (d.request_valid_o) return; }
        require(false,"bus watchdog");
    }
    void send(unsigned stalls=2) {
        for (unsigned n=0;n<stalls;++n) { d.issue_allowed_i=0; tick(); }
        d.request_ready_i=1; tick(); d.request_ready_i=0; require(sent,"request acceptance");
    }
    void response(unsigned status=0) {
        cause=status ? (op.store ? 7:5):0;
        for (unsigned n=0;n<(RESPONSE_BITS+31)/32;++n) d.response_i[n]=0;
        put(d.response_i,RESPONSE_TRANSACTION_ID_OFFSET,4,ids[op.client()]); put(d.response_i,RESPONSE_STATUS_OFFSET,2,status);
        if (!status && !op.store) {
            if (op.uncached) {
                uint32_t lanes=0;
                for (unsigned b=0;b<op.bytes();++b) lanes|=uint32_t(memory[(op.address+b)%256])<<(8*((op.address+b)%4));
                put(d.response_i,RESPONSE_UNCACHED_READ_DATA_OFFSET,32,lanes);
            } else for (unsigned b=0;b<32;++b)
                put(d.response_i,RESPONSE_LINE_READ_DATA_OFFSET+8*b,8,memory[((op.address&~31U)+b)%256]);
        }
        d.response_valid_i=1; d.eval(); require(d.response_ready_o,"target response ready"); tick(); d.response_valid_i=0;
        ids[op.client()]=(ids[op.client()]+1)%16; ++count["responses"];
        if (!status && op.store) for (unsigned b=0;b<op.bytes();++b) memory[(op.address+b)%256]=uint8_t(op.source2>>(8*b));
        if (!owned) { drain=committed=false; offered=sent=false; }
    }
    void finish() {
        head(); await_result(); independence(!(op.store && !op.uncached && !cause));
        for (unsigned n=0;n<4;++n) {
            d.source_ready_i=n; d.source1_i=rng(); d.source2_i=rng();
            tick(); require(cause ? d.fault_offer_o:d.serial_offer_o,"result stall");
        }
        for (unsigned bit=0;bit<13;++bit) {
            d.head_id_i=op.id^(1U<<bit); tick(); require(!d.serial_offer_o && !d.fault_offer_o,"result head identity");
        }
        head(); d.head_pc_i^=4; tick(); require(!d.serial_offer_o && !d.fault_offer_o,"result head PC");
        head(); d.head_valid_i=0; tick(); require(!d.serial_offer_o && !d.fault_offer_o,"result head valid");
        head(); ++count["result_head_blocked"];
        if (cause) {
            d.fault_accept_i=1; tick(); d.fault_accept_i=0; completed=true; ++count["fault_completion"];
            d.eval(); require(d.trap_ready_o && !d.fault_offer_o,"completion retains result until trap"); independence();
            for (unsigned n=0;n<3;++n) { tick(); require(d.trap_ready_o && !d.fault_offer_o,"trap stall and no repeat completion"); }
            d.head_id_i=op.id^32; tick(); require(!d.trap_ready_o,"trap full identity"); head();
            d.head_pc_i^=4; tick(); require(!d.trap_ready_o,"trap PC"); head();
            d.head_valid_i=0; tick(); require(!d.trap_ready_o,"trap head valid"); head();
            d.recovery_i=1; d.trap_accept_i=1; tick(); d.recovery_i=0; d.trap_accept_i=0; owned=false; ++count["traps"];
        } else accept_serial();
        if (!drain) { offered=sent=false; controls(); tick(); require(!d.busy_o,"final release"); }
    }
    void normal(Operation x,unsigned status=0,bool checks=false,unsigned stalls=2) {
        start(x,checks); if (checks) block_owner();
        if (op.store && !op.uncached && !cause) {
            tick(); require(!d.serial_offer_o && !d.request_valid_o,"no admission");
            d.admit_valid_i=1;
            for (unsigned bit=0;bit<13;++bit) { d.admit_id_i=op.id^(1U<<bit); tick(); require(!d.serial_offer_o,"full admission identity"); }
            d.admit_id_i=op.id; d.eval(); require(d.serial_offer_o,"late admission");
            independence(false); d.admit_valid_i=0; tick(); require(!d.serial_offer_o,"withdrawn admission");
            d.admit_valid_i=1; d.recovery_i=1; tick(); require(!d.serial_offer_o,"recovery blocks commit"); d.recovery_i=0;
            finish(); controls();
            auto next=make(); descriptor(next); d.head_valid_i=1; d.head_id_i=next.id; d.head_pc_i=next.pc;
            for (unsigned n=0;n<3;++n) { tick(); require(!d.read_request_o && !d.prepare_o,"committed drain blocks preparation"); }
            ++count["drain_blocked"];
            d.recovery_i=1; d.cancel_i=1; offer(); send(stalls); response();
            controls(); tick(); require(!d.busy_o,"committed drain release"); ++count["committed_drain"];
        } else if (cause) finish();
        else {
            head(); d.issue_allowed_i=0;
            for (unsigned n=0;n<3;++n) { tick(); require(!d.request_valid_o,"issue permission"); }
            ++count["issue_blocked"];
            d.issue_allowed_i=1; d.head_valid_i=0;
            for (unsigned n=0;n<2;++n) { tick(); require(!d.request_valid_o,"ungranted head valid"); }
            head(); d.head_pc_i^=4;
            for (unsigned n=0;n<2;++n) { tick(); require(!d.request_valid_o,"ungranted head PC"); }
            head(); d.head_id_i^=4096;
            for (unsigned n=0;n<2;++n) { tick(); require(!d.request_valid_o,"ungranted head generation"); }
            head(); ++count["ungranted_head_blocked"];
            offer(); send(stalls); for (unsigned n=0;n<stalls;++n) tick(); response(status); finish();
        }
    }
    void cancel_case(unsigned phase,bool store=false) {
        start(make(store));
        if (phase==1) tick();
        if (phase>=2) offer();
        if (phase>=3) send();
        if (phase==4) response();
        d.cancel_i=1; tick(); d.cancel_i=0; owned=false;
        if (phase==2 || phase==3) {
            drain=true; auto next=make(); descriptor(next); d.head_id_i=next.id; d.head_pc_i=next.pc;
            tick(); require(!d.prepare_o,"canceled read drain blocks next owner");
            if (phase==2) send();
            response();
        }
        offered=sent=false; controls(); tick(); require(!d.busy_o,"cancel release"); ++count["cancel_"+std::to_string(phase)];
    }
    void unexpected(unsigned phase) {
        reset();
        if (phase==0) { start(make(true)); d.admit_valid_i=1; d.admit_id_i=op.id; tick(); }
        else if (phase<=3) {
            start(make(false,0,REGIONS[1].base+1)); offer(); send(); response(phase==1 ? 0:1); head(); await_result();
            if (phase==3) { d.fault_accept_i=1; tick(); d.fault_accept_i=0; completed=true; }
        } else if (phase==4) { start(make()); }
        else { op=make(); descriptor(op); head(); }
        d.response_valid_i=1; d.eval();
        require(!d.serial_offer_o && !d.fault_offer_o && !d.trap_ready_o,"raw response blocks acceptance edge");
        tick(); fatal=true; d.admit_valid_i=0;
        for (unsigned n=0;n<4;++n) { d.cancel_i=!d.irrevocable_o; d.recovery_i=1; tick(); }
        require(d.fatal_sources_o==8 && d.fatal_reason_o==64,"unsolicited response diagnostics");
        ++count["response_race_"+std::to_string(phase)]; reset();
    }
    void directed() {
        for (bool store:{false,true}) for (unsigned kind:{0U,1U,2U,4U,5U}) {
            if (store && kind>=4) continue;
            for (unsigned region=0;region<=REGIONS.size();++region) for (unsigned lane=0;lane<4;++lane) {
                uint32_t base=region<REGIONS.size() ? REGIONS[region].base:0xfffff000U;
                normal(make(store,kind,base+lane),0,true); ++count["region_"+std::to_string(region)];
            }
            for (unsigned alias=0;alias<4;++alias) {
                auto x=make(store,kind,64,64,alias==0 ? 0:3,alias==1 ? 0:3,alias==2 ? 0:3);
                if (alias==3 && store) x.source2=x.source1;
                normal(x); ++count["alias_"+std::to_string(alias)];
            }
            if (!store) for (unsigned region=0;region<REGIONS.size();++region)
                normal(make(false,kind,REGIONS[region].base),1);
            else normal(make(true,kind,REGIONS[1].base),1);
        }
        for (unsigned phase=0;phase<5;++phase) cancel_case(phase);
        cancel_case(0,true);
        start(make(true)); d.admit_valid_i=1; d.admit_id_i=op.id; d.eval(); require(d.serial_offer_o,"cancel ready store setup");
        d.cancel_i=1; tick(); owned=false; controls(); tick(); require(!d.busy_o,"cancel ready store"); ++count["cancel_ready_store"];
        for (unsigned local=0;local<2;++local) {
            start(make(false,2,local ? 65:64));
            if (!local) { offer(); send(); response(1); }
            await_result();
            if (!local) { d.fault_accept_i=1; tick(); d.fault_accept_i=0; completed=true; }
            d.cancel_i=1; tick(); owned=false; offered=sent=false;
            controls(); tick(); require(!d.busy_o,"cancel revocable fault"); ++count["cancel_fault"];
            normal(make(false,2,65));
        }
        for (unsigned phase=0;phase<6;++phase) unexpected(phase);
        start(make()); offer(); send(); response(); await_result(); accept_serial();
        normal(make(false,0,REGIONS[1].base)); ++count["back_to_back"];
        for (unsigned phase=0;phase<6;++phase) {
            start(make(false,0,REGIONS[1].base));
            if (phase>=1) tick();
            if (phase>=2) offer();
            if (phase>=3) send();
            if (phase>=4) response(1);
            if (phase==5) { await_result(); d.fault_accept_i=1; tick(); d.fault_accept_i=0; completed=true; }
            reset(); ++count["reset_phase_"+std::to_string(phase)];
        }
        start(make(true)); d.admit_valid_i=1; d.admit_id_i=op.id; accept_serial(); reset(); ++count["reset_committed"];
    }
};
int main(int argc,char** argv) {
    Verilated::commandArgs(argc,argv);
    try {
        unsigned seed=argc>1 && std::string(argv[1])!="negative" ? std::stoul(argv[1]):1;
        Bench b(seed);
        if (argc>1 && std::string(argv[1])=="negative") {
            std::string name=argv[2];
            if (name=="serial") b.d.serial_accept_i=1;
            else if (name=="fault") b.d.fault_accept_i=1;
            else if (name=="trap") b.d.trap_accept_i=1;
            else if (name=="cancel-accept") { b.d.cancel_i=1; b.d.serial_accept_i=1; }
            else if (name=="admission") b.d.admit_valid_i=1;
            else if (name=="release") {
                b.start(b.make()); b.offer(); b.send(); b.response(); b.await_result(); b.accept_serial();
                b.offered=b.sent=false; b.descriptor(b.op);
            } else if (name=="mmio-cancel" || name=="faulted-mmio-cancel") {
                b.start(b.make(false,0,REGIONS[1].base)); b.offer();
                if (name=="faulted-mmio-cancel") { b.send(); b.response(1); b.await_result(); b.d.fault_accept_i=1; b.tick(); b.d.fault_accept_i=0; b.completed=true; }
                b.d.cancel_i=1;
            } else throw std::runtime_error("unknown negative");
            b.d.clk_i=0; b.d.eval(); b.d.clk_i=1; b.d.eval();
            throw std::runtime_error("negative survived");
        }
        unsigned random=argc>2 ? std::stoul(argv[2]):500;
        b.directed();
        for (unsigned n=0;n<random;++n) {
            bool store=b.rng()%2; unsigned kind=b.rng()%3;
            if (!store && kind<2 && b.rng()%2) kind+=4;
            auto r=REGIONS[b.rng()%REGIONS.size()]; uint32_t address=r.base+(b.rng()%32);
            b.normal(b.make(store,kind,address,int(b.rng()%4096)-2048),0,false,b.rng()%4);
        }
        std::cout<<"HEAD MEMORY PASS seed="<<seed<<" random="<<random<<" cases="<<b.cases<<" cycles="<<b.cycles;
        for (const auto& [key,value]:b.count) std::cout<<" "<<key<<"="<<value;
        std::cout<<"\n";
    } catch (const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
