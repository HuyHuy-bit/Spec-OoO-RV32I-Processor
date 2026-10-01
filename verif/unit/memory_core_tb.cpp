#include "fetch_execution_reference.hpp"
#include "platform_memory_layout.hpp"

struct MemoryBench : Bench {
    std::map<uint32_t,uint8_t> architectural, target;
    std::array<unsigned,3> ids{};
    std::array<uint32_t,(REQUEST_BITS+31)/32> packet{};
    Expected result{};
    bool prepared=false, store=false, uncached=false, committed=false;
    bool offered=false, accepted=false, answered=false, canceled=false;
    bool watch_older_fault=false, watch_branch_fence=false, younger_dispatched=false, bad_response=false, data_ready=true, issue_allowed=true, admit=true, wrong_admit=false;
    unsigned allocations=0, watched_allocations=0, watch_pc=20, data_latency=1, remaining=0, kind=0, bytes=0, cause=0, mask=0, client=0;
    uint32_t address=0, data=0, fault_address=UINT32_MAX;
    MemoryBench() { memory_service=system_service=trap_service=fault_support=true; }
    void drive_memory(const Input&) override {
        d.memory_issue_allowed_i=issue_allowed;
        d.admit_valid_i=admit && d.admission_valid_o;
        d.admit_id_i=d.admission_id_o ^ (wrong_admit ? 32:0);
        d.data_request_ready_i=data_ready;
        d.data_response_valid_i=bad_response || (accepted && !answered && !remaining);
        if (d.data_response_valid_i) {
            put(d.data_response_i,RESPONSE_TRANSACTION_ID_OFFSET,4,ids[client]);
            put(d.data_response_i,RESPONSE_STATUS_OFFSET,2,bad_response ? 2:address==fault_address ? 1:0);
            if (!bad_response && !store && address!=fault_address) {
                if (uncached) {
                    uint32_t word=0;
                    for (unsigned n=0;n<bytes;n++) word|=uint32_t(target[address+n])<<(8*((address+n)%4));
                    put(d.data_response_i,RESPONSE_UNCACHED_READ_DATA_OFFSET,32,word);
                } else for (unsigned n=0;n<32;n++)
                    put(d.data_response_i,RESPONSE_LINE_READ_DATA_OFFSET+8*n,8,target[(address&~31U)+n]);
            }
        }
    }
    void prepare() {
        require(!prepared && (!accepted || answered) && pc<65536,"single live memory owner");
        const auto insn=memory[pc/4]; const unsigned opcode=insn&127;
        require((opcode==3 || opcode==0x23) && !illegal(insn),"architectural memory head");
        require(!d.retire_accept_o,"memory preparation before older retirement");
        require(target==architectural,"new memory owner before committed store drain");
        store=opcode==0x23; kind=(insn>>12)&7; bytes=1U<<(kind%4);
        const unsigned a=(insn>>15)&31, b=store ? (insn>>20)&31:0, rd=store ? 0:(insn>>7)&31;
        require(known[a] && known[b],"memory source initialized");
        const auto imm=store ? ((insn>>25)<<5)|((insn>>7)&31):insn>>20;
        address=regs[a]+extend(imm,12); data=regs[b]; cause=0; uncached=false;
        bool permitted=false;
        for (const auto& r:REGIONS) if (address>=r.base && uint64_t(address)+bytes<=uint64_t(r.base)+r.size && (store ? r.write:r.read)) {
            permitted=true; uncached=!r.cacheable;
        }
        if (address%bytes) cause=store ? 6:4;
        else if (!permitted || address==fault_address) cause=store ? 7:5;
        mask=((1U<<bytes)-1)<<(address%4); client=store ? (uncached ? 2:1):0;
        result={}; result.op=store ? 33:32; result.rd=cause ? 0:rd; result.next_pc=cause ? pc:pc+4; result.fault=cause;
        auto& e=result.event;
        put(e,VALID_OFFSET,1,1); put(e,ORDER_OFFSET,64,order); put(e,PRIVILEGE_OFFSET,2,3);
        put(e,INSTRUCTION_OFFSET,32,insn); put(e,PC_BEFORE_OFFSET,32,pc); put(e,PC_AFTER_OFFSET,32,result.next_pc);
        put(e,RS1_ADDR_OFFSET,5,a); put(e,RS1_VALUE_OFFSET,32,regs[a]);
        put(e,RS2_ADDR_OFFSET,5,b); put(e,RS2_VALUE_OFFSET,32,regs[b]);
        uint32_t lanes=0;
        for (unsigned n=0;n<bytes;n++) lanes|=uint32_t((data>>(8*n))&255)<<(8*((address+n)%4));
        if (cause) {
            put(e,TRAP_OFFSET,1,1); put(e,TRAP_CAUSE_OFFSET,32,cause); put(e,TRAP_VALUE_OFFSET,32,address);
        } else {
            put(e,RETIRED_OFFSET,1,1); put(e,MEM_VALID_OFFSET,1,1); put(e,MEM_ADDRESS_OFFSET,32,address);
            if (store) { put(e,MEM_WRITE_MASK_OFFSET,4,mask); put(e,MEM_WRITE_DATA_OFFSET,32,lanes); }
            else {
                uint32_t value=0, read_lanes=0;
                for (unsigned n=0;n<bytes;n++) { auto byte=architectural[address+n]; value|=uint32_t(byte)<<(8*n); read_lanes|=uint32_t(byte)<<(8*((address+n)%4)); }
                if (kind==0) value=extend(value,8);
                if (kind==1) value=extend(value,16);
                result.result=value;
                put(e,MEM_READ_MASK_OFFSET,4,mask); put(e,MEM_READ_DATA_OFFSET,32,read_lanes);
                put(e,RD_ADDR_OFFSET,5,rd); put(e,RD_VALUE_OFFSET,32,rd ? value:0); put(e,RD_WRITE_MASK_OFFSET,32,rd ? UINT32_MAX:0);
            }
        }
        packet={}; put(packet,REQUEST_TRANSACTION_ID_OFFSET,4,ids[client]);
        put(packet,REQUEST_WRITE_OFFSET,1,store); put(packet,REQUEST_UNCACHED_OFFSET,1,uncached);
        put(packet,REQUEST_ADDRESS_OFFSET,32,uncached ? address:(address&~31U));
        if (uncached) {
            put(packet,REQUEST_UNCACHED_SIZE_OFFSET,2,kind%4);
            if (store) { put(packet,REQUEST_UNCACHED_WRITE_DATA_OFFSET,32,lanes); put(packet,REQUEST_UNCACHED_WRITE_STROBE_OFFSET,4,mask); }
        } else if (store) {
            for (unsigned n=0;n<bytes;n++) put(packet,REQUEST_LINE_WRITE_DATA_OFFSET+8*((address+n)%32),8,(data>>(8*n))&255);
            put(packet,REQUEST_LINE_WRITE_MASK_OFFSET,32,((1U<<bytes)-1)<<(address%32));
        }
        prepared=true; committed=offered=accepted=answered=canceled=false;
        coverage["memory_prepare"]++;
        coverage[(store ? "store_":"load_")+std::to_string(kind)]++;
        coverage["byte_offset_"+std::to_string(address%4)]++;
        if (!a) coverage["zero_base"]++;
        if (store && !b) coverage["zero_store"]++;
        if (!store && !rd) coverage["zero_load"]++;
        if (!store && a==rd && rd) coverage["load_alias"]++;
        if (store && a==b && a) coverage["store_alias"]++;
    }
    void observe_memory(const Input& i) override {
        if (i.reset) {
            prepared=offered=accepted=answered=committed=canceled=false; ids={}; allocations=watched_allocations=0;
            architectural.clear(); target.clear();
            for (unsigned n=0;n<65536;n++) architectural[n]=uint8_t(memory[n/4]>>(8*(n%4)));
            for (unsigned n=0;n<32;n++) architectural[0x10001000+n]=uint8_t(0x91+n);
            for (unsigned n=0;n<16;n++) architectural[0x10000000+n]=uint8_t(0xe1+n);
            target=architectural;
            require(!d.data_request_valid_o && !d.memory_prepare_o,"memory reset visibility");
            return;
        }
        if ((watch_older_fault || watch_branch_fence) && (d.dispatch_o&1) && uint32_t(d.fetch_pc_o)==watch_pc) {
            younger_dispatched=true; watched_allocations=allocations;
        }
        allocations+=(d.dispatch_o&1)+((d.dispatch_o>>1)&1);
        if (watch_older_fault && d.trap_accept_o) require(younger_dispatched,"older fault tested with younger memory dispatched");
        require(bool(d.memory_committed_o)==committed,"committed store ownership output");
        if (pc<65536 && fence(memory[pc/4]) && d.occupancy_o && !system_prepared && d.memory_busy_o) coverage["fence_order_wait"]++;
        if (d.system_prepare_o && pc<65536 && fence(memory[pc/4])) {
            require(!prepared && !committed && (!accepted || answered) && target==architectural,"FENCE prepared before older memory completion");
            coverage["fence_prepare"]++;
        }
        if (fatal && !offered && !committed) require(!d.data_request_valid_o,"new data request after frontend fatal");
        if (data_fatal && !held) require(!d.request_valid_o,"new instruction request after data fatal");
        if (bad_response) require(!d.trap_accept_o,"trap accepted with malformed data response");
        require(bool(d.flush_ready_o)==!bool(d.memory_irrevocable_o),"flush permission");
        if (d.memory_prepare_o) prepare();
        if (prepared && !i.flush) require(!d.dispatch_o,"younger dispatch past memory barrier");
        if (d.admission_valid_o) {
            require(prepared && store && !uncached && !cause,"unexpected admission");
            uint32_t lanes=0; for (unsigned n=0;n<bytes;n++) lanes|=((data>>(8*n))&255)<<(8*((address+n)%4));
            require(d.admission_address_o==address && d.admission_data_o==lanes && d.admission_mask_o==mask && d.admission_size_o==kind%4,"admission descriptor");
            if (!admit || wrong_admit) { require(!d.retire_accept_o,"unadmitted store retirement"); coverage[wrong_admit ? "wrong_admission":"admission_stall"]++; }
        }
        if (offered && !accepted) require(d.data_request_valid_o,"data request withdrawn");
        if (d.data_request_valid_o) {
            require(!accepted && (prepared || committed || canceled),"duplicate or unowned data request");
            require(!cause || address==fault_address,"local fault issued traffic");
            if (store && !uncached) require(committed,"cached store before retirement");
            for (unsigned n=0;n<REQUEST_BITS;n++) require(bit(d.data_request_o,n)==bit(packet,n),"data request bit="+std::to_string(n));
            offered=true;
            if (data_ready) { accepted=true; remaining=data_latency; coverage["data_requests"]++; }
            else coverage["data_request_stall"]++;
        } else if (accepted && remaining) --remaining;
        if (!bad_response && d.data_response_valid_i && d.data_response_ready_o) {
            require(accepted && !answered,"unexpected data response");
            answered=true; ids[client]=(ids[client]+1)%16; coverage["data_responses"]++;
            if (store && !cause) for (unsigned n=0;n<bytes;n++) target[address+n]=uint8_t(data>>(8*n));
            if (committed) { require(target==architectural,"committed bytes differ"); committed=false; }
        }
        if (prepared && d.retire_valid_o && !cause) {
            require(d.retire_valid_o==1,"memory retirement not solo"); compare(d.retire_event_o,0,result.event);
            if (!d.retire_accept_o) coverage["memory_result_stall"]++;
        }
        if (i.flush && prepared) {
            require(!uncached || !offered,"flush during MMIO ownership");
            prepared=false; canceled=offered; coverage["memory_cancel"]++;
        }
        if (d.memory_irrevocable_o) { require(!d.flush_ready_o,"irrevocable flush exposed"); coverage["irrevocable"]++; }
        if (committed) coverage["committed_drain"]++;
    }
    Expected expected() override {
        if (pc<65536 && !illegal(memory[pc/4]) && ((memory[pc/4]&127)==3 || (memory[pc/4]&127)==0x23)) {
            require(prepared,"memory event without preparation"); return result;
        }
        return Bench::expected();
    }
    void memory_retired(const Expected& e) override {
        if (e.op==34) {
            require(!prepared && !committed && (!accepted || answered) && target==architectural,"FENCE retired before older memory completion");
            return;
        }
        if (e.op!=32 && e.op!=33) return;
        require(prepared,"memory acceptance owner");
        if (store) {
            for (unsigned n=0;n<bytes;n++) architectural[address+n]=uint8_t(data>>(8*n));
            if (uncached) require(answered && target==architectural,"MMIO store completion before retirement");
            else { committed=true; coverage["store_commit"]++; }
        }
        prepared=false; coverage["memory_retired"]++;
    }
    void memory_trapped() override {
        if (!prepared) return;
        require(cause,"memory trap on success"); prepared=false;
        coverage["memory_trap_"+std::to_string(cause)]++;
    }
};
static uint32_t mem(bool store,unsigned kind,unsigned rd,unsigned rs1,unsigned rs2,int offset=0) {
    const unsigned imm=unsigned(offset)&4095;
    return store ? (imm>>5)<<25|rs2<<20|rs1<<15|kind<<12|(imm&31)<<7|0x23
                 : imm<<20|rs1<<15|kind<<12|rd<<7|3;
}
static void initialize(MemoryBench& b) {
    b.memory.fill(0x0000100f);
    for (unsigned n=0x4000/4;n<0x4100/4;n++) b.memory[n]=0x81fe80ffU+n;
    b.memory[0]=instruction(0,1,0,0,0x4000);
    b.memory[1]=instruction(0,2,0,0,0x87654000);
    b.memory[2]=instruction(2,2,2,0,0x321);
    b.memory[3]=instruction(0,3,0,0,0x10000000);
    b.watch_older_fault=b.watch_branch_fence=b.younger_dispatched=b.bad_response=false; b.watch_pc=20;
    b.issue_allowed=b.admit=b.data_ready=true; b.wrong_admit=false; b.fault_address=UINT32_MAX;
}
static bool terminal(const MemoryBench& b,uint32_t stop) {
    return b.pc==stop && !b.d.occupancy_o && !b.d.memory_busy_o && !b.d.fetch_busy_o
        && (b.d.unsupported_o&1) && uint32_t(b.d.fetch_pc_o)==stop;
}
template<class Stimulus>
static void run_until(MemoryBench& b,uint32_t stop,Stimulus stimulus) {
    for (unsigned n=0;n<15000;n++) {
        b.tick(stimulus(n));
        b.require(!b.fatal,"unexpected fatal");
        if (terminal(b,stop)) {
            b.require(!b.prepared && !b.committed && !b.system_prepared && (!b.offered || b.answered)
                && b.target==b.architectural,"terminal memory ownership or state mismatch");
            return;
        }
    }
    b.require(false,"memory program watchdog pc="+std::to_string(b.pc)
        +" occupancy="+std::to_string(b.d.occupancy_o)+" memory_busy="+std::to_string(b.d.memory_busy_o)
        +" fetch_busy="+std::to_string(b.d.fetch_busy_o)+" unsupported="+std::to_string(b.d.unsupported_o));
}
static void run(MemoryBench& b,uint32_t stop,std::mt19937& rng) {
    run_until(b,stop,[&](unsigned) {
        Input i; i.trap_ready=rng()%3!=0; i.request_ready=rng()%3!=0; i.latency=rng()%5;
        i.retire=rng()%4; i.execute=rng()%4; i.complete=rng()%4; i.grant=rng()%3!=0;
        b.data_ready=rng()%3!=0; b.data_latency=rng()%9;
        b.issue_allowed=rng()%3!=0; b.admit=rng()%3!=0; b.wrong_admit=rng()%5==0;
        return i;
    });
}
static void drain_setup(MemoryBench& b) {
    initialize(b); b.memory[4]=mem(true,2,0,1,2); b.reset();
    b.data_ready=false; b.data_latency=30;
    for (unsigned n=0;n<1000 && !b.committed;n++) b.tick();
    b.require(b.committed,"drain committed-store setup");
    Input i; i.enable=false; i.flush=true; i.target=0x800; b.tick(i); i.flush=false;
    b.tick(i); b.tick(i);
    b.require(!b.d.fetch_busy_o && !b.d.fetch_valid_o && !b.d.occupancy_o && b.d.memory_busy_o,"isolated memory drain holder");
}
int main(int argc,char** argv) {
    Verilated::commandArgs(argc,argv);
    try {
        const bool negative=argc>1 && std::string(argv[1])=="negative";
        const unsigned seed=argc>1 && !negative ? std::stoul(argv[1]):1;
        std::mt19937 rng(seed); MemoryBench b;
        if (negative && argc>2 && std::string(argv[2])=="watchdog") {
            initialize(b); b.memory[4]=mem(true,2,0,1,2); b.reset(); b.data_ready=false;
            for (unsigned n=0;n<1000 && !(b.pc==20 && !b.d.occupancy_o
                    && (b.d.unsupported_o&1) && uint32_t(b.d.fetch_pc_o)==20 && !b.d.fetch_busy_o);n++) b.tick();
            b.require(b.pc==20 && b.committed && b.d.memory_busy_o && !b.d.occupancy_o
                && (b.d.unsupported_o&1) && uint32_t(b.d.fetch_pc_o)==20 && !b.d.fetch_busy_o,"terminal guard setup");
            run_until(b,20,[&](unsigned) { b.data_ready=false; return Input{}; });
            throw std::runtime_error("terminal accepted undrained committed store");
        }
        if (negative && argc>2 && std::string(argv[2])=="drain") {
            drain_setup(b);
            b.d.clk_i=0; b.d.drained_i=1; b.d.eval(); b.d.clk_i=1; b.d.eval();
            throw std::runtime_error("missing memory drain assertion");
        }
        if (negative) {
            initialize(b); b.memory[4]=mem(false,2,4,3,0); b.reset();
            for (unsigned n=0;n<1000 && !b.d.memory_irrevocable_o;n++) { Input i; i.retire=b.prepared ? 0:3; b.tick(i); }
            b.require(b.d.memory_irrevocable_o,"negative setup");
            b.d.clk_i=0; b.d.flush_i=1; b.d.eval(); b.d.clk_i=1; b.d.eval();
            throw std::runtime_error("missing forbidden MMIO flush assertion");
        }
        for (unsigned offset=0;offset<8;offset++) {
            initialize(b); unsigned at=4;
            while (at%8!=offset) b.memory[at++]=instruction(2,0,0,0,0);
            for (unsigned region=0;region<2;region++) {
                unsigned base=region ? 3:1;
                for (unsigned kind:{0u,1u,2u,4u,5u}) for (unsigned lane=0;lane<4;lane+=(1u<<(kind%4))) {
                    b.memory[at++]=mem(false,kind,4,base,0,lane);
                    b.memory[at++]=instruction(2,5,4,0,1);
                }
                for (unsigned kind=0;kind<3;kind++) for (unsigned lane=0;lane<4;lane+=(1u<<kind)) {
                    b.memory[at++]=mem(true,kind,0,base,2,lane);
                    b.memory[at++]=mem(false,kind,4,base,0,lane);
                }
                b.memory[at++]=mem(true,2,0,base,base,4);
                b.memory[at++]=mem(true,2,0,base,0,8);
                b.memory[at++]=mem(false,2,0,base,0,8);
            }
            b.memory[at++]=mem(true,2,0,0,2,0x700);
            b.memory[at++]=mem(false,2,4,0,0,0x700);
            b.memory[at++]=mem(false,2,1,1,0);
            b.memory[at++]=csr(0xb02,2,0,6);
            b.reset(); run(b,at*4,rng); b.coverage["instruction_offset_"+std::to_string(offset)]++;
        }
        for (unsigned test=0;test<8;test++) {
            initialize(b); bool store=test%2; unsigned at=4;
            const uint32_t addr=test<2 ? 0x4001:test<4 ? 0x20000000:test<6 ? 0x4000:0x10000000;
            b.memory[at++]=instruction(0,1,0,0,addr&~4095U);
            b.memory[at++]=instruction(2,1,1,0,addr&4095);
            b.memory[at++]=instruction(2,7,0,0,0x400);
            b.memory[at++]=csr(0x305,1,7,0);
            if (test>=4) b.fault_address=addr;
            // Cached stores receive guaranteed admission, so only loads may bus-fault there.
            if (test==5) continue;
            const unsigned fault_pc=at*4;
            b.memory[at++]=mem(store,2,4,1,2);
            b.memory[at++]=csr(0xb02,2,0,9);
            unsigned h=0x400/4;
            b.memory[h++]=csr(0x342,2,0,10); b.memory[h++]=csr(0x343,2,0,11);
            b.memory[h++]=csr(0x341,2,0,12); b.memory[h++]=instruction(2,12,12,0,4);
            b.memory[h++]=csr(0x341,1,12,0); b.memory[h++]=0x30200073;
            const auto before=b.traps; b.reset(); run(b,at*4,rng);
            b.require(b.traps==before+1 && b.regs[11]==addr && b.regs[12]==fault_pc+4,"memory trap handler and MRET");
            b.require(b.regs[10]==(test<2 ? (store ? 6u:4u):(store ? 7u:5u)),"memory fault cause");
        }
        // External cancellation drains offered reads and committed writes; reset clears the fabric too.
        for (unsigned phase=0;phase<9;phase++) for (bool reset:{false,true}) {
            initialize(b); b.memory[4]=mem(phase==4 || phase==6 || phase==8,2,4,phase>=7 ? 3:1,2,phase==5 ? 1:0);
            b.memory[0x800/4]=0x0ff0000f; b.memory[0x804/4]=csr(0xb02,2,0,6);
            b.reset(); b.data_ready=phase!=1; b.data_latency=40;
            b.issue_allowed=phase!=0 && phase<7; b.admit=phase!=6;
            bool reached=false;
            for (unsigned n=0;n<1000;n++) {
                Input i; i.retire=b.prepared && phase!=4 ? 0:3; b.tick(i);
                reached=phase==0 ? b.prepared:phase==1 ? b.offered&&!b.accepted:
                    phase==2 ? b.accepted&&!b.answered:phase==3 ? b.answered:
                    phase==4 ? b.committed:phase==5 ? bool(b.d.backend_fault_o):phase==6 ? bool(b.d.admission_valid_o):b.prepared;
                if (reached) break;
            }
            b.require(reached,"cancellation phase watchdog");
            if (reset) { b.reset(); b.coverage["reset_memory_"+std::to_string(phase)]++; }
            else {
                Input i; i.flush=true; i.target=0x800; b.tick(i);
                run(b,0x808,rng); b.require(b.target==b.architectural,"cancellation memory state");
                b.coverage["flush_memory_"+std::to_string(phase)]++;
            }
        }
        for (bool fault:{false,true}) for (bool store:{false,true}) {
            initialize(b); b.memory[4]=mem(store,2,4,3,2);
            b.memory[0x100/4]=csr(0x342,2,0,10);
            if (fault) b.fault_address=0x10000000;
            b.reset();
            for (unsigned n=0;n<1000 && !(fault ? bool(b.d.trap_valid_o):bool(b.d.retire_valid_o) && b.prepared);n++) {
                Input i; i.retire=b.prepared ? 0:3; b.tick(i);
            }
            b.require(b.prepared && (fault ? bool(b.d.trap_valid_o):bool(b.d.retire_valid_o)),"MMIO held result watchdog");
            for (unsigned n=0;n<20;n++) {
                Input i; i.retire=n%2 ? 2:0; b.tick(i);
                b.require(b.d.memory_irrevocable_o && !b.d.flush_ready_o,"MMIO owner released under acceptance stall");
            }
            run(b,fault ? 0x104:20,rng);
            b.require(b.d.flush_ready_o && !b.d.memory_busy_o,"MMIO ownership release");
            b.coverage[fault ? "mmio_fault_hold":"mmio_success_hold"]++;
        }
        // A younger memory descriptor must not block the trap of an older fault.
        initialize(b); b.memory[4]=0xffffffff; b.memory[5]=mem(false,2,4,1,0);
        b.memory[0x100/4]=csr(0x342,2,0,10); b.reset(); b.watch_older_fault=true;
        for (unsigned n=0;n<1000 && !b.younger_dispatched;n++) b.tick();
        b.require(b.younger_dispatched,"younger memory allocation watchdog"); run(b,0x104,rng);
        b.require(b.regs[10]==2 && !b.prepared,"older fault behind memory descriptor");
        b.coverage["older_fault_memory"]++;
        for (unsigned phase=0;phase<4;phase++) {
            initialize(b); b.memory[4]=mem(phase>=2,2,4,phase==0 || phase==3 ? 1:3,2);
            b.reset(); b.data_latency=30;
            bool reached=false;
            for (unsigned n=0;n<1000;n++) { Input i; i.retire=b.prepared && phase!=3 ? 0:3; b.tick(i); if (b.accepted) { reached=true; break; } }
            b.require(reached,"frontend fatal memory setup");
            Input bad; bad.inject_bad=true; bad.retire=0; b.tick(bad);
            for (unsigned n=0;n<80;n++) { Input i; i.retire=0; b.tick(i); }
            b.require(b.answered && (phase==3 ? !b.d.memory_busy_o:bool(b.d.memory_busy_o)) && !b.d.retire_accept_o,"fatal preserves memory ownership and drains bus");
            b.coverage["frontend_fatal_memory_"+std::to_string(phase)]++;
        }
        for (bool store:{false,true}) {
            initialize(b); b.memory[4]=mem(store,2,4,3,2); b.reset(); b.issue_allowed=false;
            for (unsigned n=0;n<1000 && !b.prepared;n++) b.tick();
            b.require(b.prepared && !b.offered,"pregrant fatal setup");
            Input bad; bad.inject_bad=true; b.tick(bad); b.issue_allowed=true;
            for (unsigned n=0;n<20;n++) b.tick();
            b.require(!b.offered,"MMIO launched after frontend fatal"); b.coverage["pregrant_fatal"]++;
        }
        initialize(b); b.memory[4]=mem(false,2,4,3,0); b.fault_address=0x10000000; b.reset();
        for (unsigned n=0;n<1000 && !b.d.trap_valid_o;n++) b.tick();
        b.require(b.d.trap_valid_o,"held fault protocol error setup");
        b.bad_response=true; { Input i; i.trap_ready=true; b.tick(i); } b.bad_response=false; b.data_fatal=true;
        b.tick(); b.require(b.d.memory_fatal_o && b.d.memory_busy_o,"held fault frozen on protocol fatal");
        b.coverage["held_fault_protocol_error"]++;
        initialize(b);
        for (unsigned n=4;n<7;n++) b.memory[n]=instruction(2,0,0,0,0);
        b.memory[7]=mem(false,2,4,1,0); b.reset();
        for (unsigned n=0;n<1000 && !b.accepted;n++) { b.data_latency=80; Input i; i.enable=b.requests==0 || b.pc<4; b.tick(i); }
        b.require(b.accepted && !b.pending && !b.held && !b.d.fetch_valid_o,"data fatal empty frontend setup"); b.bad_response=true; { Input i; i.enable=false; b.tick(i); } b.data_fatal=true; b.bad_response=false;
        for (unsigned n=0;n<20;n++) b.tick();
        b.require(b.d.memory_fatal_o && b.d.memory_busy_o,"data protocol fatal freeze");
        b.coverage["data_fatal"]++;
        drain_setup(b); b.data_ready=true;
        for (unsigned n=0;n<1000 && b.d.memory_busy_o;n++) { Input i; i.enable=false; b.tick(i); }
        b.require(!b.d.memory_busy_o,"committed drain progress");
        { Input i; i.enable=false; i.drain=true; b.tick(i); }
        b.coverage["memory_drain"]++;
        // FENCE after each older memory class, under bus stalls and retire backpressure.
        for (unsigned test=0;test<12;test++) {
            initialize(b); unsigned at=4;
            if (test%4<3) b.memory[at++]=mem(test%4!=2,2,4,test%4==1 ? 3:1,2);
            b.memory[at++]=test%2 ? 0xffff808f:0x0ff0000f;
            if (test>=8) b.memory[at++]=0x8330000f;
            b.memory[at++]=csr(0xb02,2,0,6);
            b.reset(); b.data_ready=test<4; b.data_latency=60;
            const auto fences=b.coverage["fence_retired"];
            run_until(b,at*4,[&](unsigned n) {
                Input i; i.retire=test>=4 && test<8 && b.system_prepared ? n%3==0:3;
                b.data_ready=test<4 || n%5==0; return i;
            });
            b.require(b.regs[6]==4+(test%4<3)+1+(test>=8)
                && b.coverage["fence_retired"]==fences+1+(test>=8),"FENCE retirement count");
            b.coverage[test%4==3 ? "fence_empty":test%4==2 ? "fence_after_load":test%4==1 ? "fence_after_mmio":"fence_after_store"]++;
            if (test>=8) b.coverage["fence_back_to_back"]++;
        }
        // Flush or reset while FENCE waits for order or is held at retirement; older faults beat a younger FENCE.
        for (unsigned held=0;held<2;held++) for (unsigned cancel=0;cancel<3;cancel++) {
            initialize(b); b.memory[4]=mem(true,2,0,1,2); b.memory[5]=0x0ff0000f;
            b.memory[0x800/4]=0x0ff0000f; b.memory[0x804/4]=csr(0xb02,2,0,6);
            b.reset(); b.data_latency=60;
            unsigned reached=0;
            for (unsigned n=0;n<2000 && reached<4;n++) {
                Input i; i.retire=held && b.system_prepared ? 0:3; b.tick(i);
                if (held ? b.system_prepared:b.pc==20 && b.committed && b.d.occupancy_o && b.d.memory_busy_o && !b.system_prepared) reached++;
            }
            b.require(reached==4,"FENCE cancellation watchdog");
            if (cancel==1) b.reset();
            else if (cancel==0) { Input i; i.flush=true; i.target=0x800; b.tick(i); run(b,0x808,rng); }
            else {
                b.bad_response=true; b.tick(); b.bad_response=false; b.data_fatal=true;
                const auto retired=b.retired;
                for (unsigned n=0;n<40;n++) { Input i; i.retire=3; b.tick(i); }
                b.require(b.retired==retired && b.d.memory_fatal_o && !b.d.retire_accept_o,"FENCE retired after data fatal");
            }
            b.coverage[std::string(held ? "fence_held_":"fence_wait_")+(cancel==1 ? "reset":cancel ? "fatal":"flush")]++;
        }
        initialize(b); b.memory[4]=mem(true,2,0,1,2); b.memory[5]=0xffffffff; b.memory[6]=0x0ff0000f;
        b.memory[0x100/4]=csr(0x342,2,0,10); b.reset(); b.watch_older_fault=true; b.watch_pc=24; b.data_latency=60;
        for (unsigned n=0;n<1000 && !b.younger_dispatched;n++) { b.data_ready=false; b.tick(); }
        b.require(b.younger_dispatched && b.committed,"younger FENCE allocation watchdog"); run(b,0x104,rng);
        b.require(b.regs[10]==2,"older fault behind FENCE"); b.coverage["older_fault_fence"]++;
        // Hold an older branch until the wrong-path fence owns the next ROB slot.
        for (unsigned pad:{0u,27u}) {
            initialize(b); unsigned at=4;
            for (unsigned n=0;n<pad;n++) b.memory[at++]=instruction(2,0,0,0,0);
            const unsigned branch_pc=at*4;
            b.memory[at++]=instruction(21,0,0,0,16);
            b.memory[at++]=0x0ff0000f;
            b.memory[at++]=instruction(2,8,0,0,0x66); b.memory[at++]=instruction(2,8,0,0,0x77);
            b.memory[at++]=0x8330000f;
            b.memory[at++]=instruction(2,8,0,0,0x55); b.memory[at++]=csr(0xb02,2,0,6);
            const auto retired=b.retired, prepares=b.coverage["fence_prepare"], fences=b.coverage["fence_retired"], redirects=b.coverage["branch_redirect"];
            b.reset(); b.watch_branch_fence=true; b.watch_pc=branch_pc+4;
            Input held; held.grant=false;
            for (unsigned n=0;n<1000 && !(b.younger_dispatched && b.pc==branch_pc && b.d.occupancy_o==2);n++) b.tick(held);
            b.require(b.younger_dispatched && b.watched_allocations==pad+5 && b.allocations==pad+6
                && b.pc==branch_pc && b.d.occupancy_o==2,"wrong-path FENCE allocation setup");
            for (unsigned n=0;n<20;n++) {
                b.tick(held);
                b.require(b.pc==branch_pc && b.d.occupancy_o==2 && b.allocations==pad+6 && !b.system_prepared
                    && b.coverage["fence_prepare"]==prepares && b.coverage["fence_retired"]==fences,"wrong-path FENCE held barrier");
            }
            for (unsigned n=0;n<100 && b.coverage["branch_redirect"]==redirects;n++) b.tick();
            b.require(b.coverage["branch_redirect"]==redirects+1,"FENCE branch recovery watchdog");
            run(b,at*4,rng);
            b.require(b.coverage["fence_prepare"]==prepares+1 && b.coverage["fence_retired"]==fences+1
                && b.coverage["branch_redirect"]==redirects+1 && b.regs[8]==0x55
                && b.regs[6]==pad+7 && b.retired==retired+pad+8,"FENCE squash, reuse and retirement count");
            b.coverage[pad ? "fence_branch_squash_wrap":"fence_branch_squash"]++;
        }
        for (unsigned round=0;round<20;round++) {
            initialize(b); unsigned at=4;
            for (unsigned n=0;n<100;n++) {
                bool store=rng()%2; unsigned kind=store ? rng()%3:std::array<unsigned,5>{0,1,2,4,5}[rng()%5];
                unsigned offset=(rng()%16)&~((1u<<(kind%4))-1);
                b.memory[at++]=mem(store,kind,4,1,2,offset);
                b.memory[at++]=instruction(2,2,2,0,int(rng()%31)-15);
                if (n%7==0) { b.memory[at++]=instruction(27,0,0,0,8); b.memory[at++]=mem(true,2,0,3,2); }
                if (rng()%4==0) b.memory[at++]=std::array<uint32_t,4>{0x0ff0000f,0x0000000f,0xffff808f,0x8330000f}[rng()%4];
            }
            b.memory[at++]=csr(0xb02,2,0,6);
            b.reset(); run(b,at*4,rng); b.coverage["random_program"]++;
        }
        std::cout<<"MEMORY CORE PASS seed="<<seed<<" cycles="<<b.cycles<<" retired="<<b.retired<<" traps="<<b.traps;
        for (const auto& [name,count]:b.coverage) std::cout<<" "<<name<<"="<<count;
        std::cout<<"\n";
    } catch(const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
