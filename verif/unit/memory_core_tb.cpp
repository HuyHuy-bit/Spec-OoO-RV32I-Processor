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
    // check retired loads against architectural bytes and backing memory after a cache clean.
    bool dcache=false, ext_offered=false, ext_write=false, ext_uncached=false, writeback_fatal_next=false;
    unsigned ext_id=0, ext_size=0, ext_strobe=0, port_transactions=0, prepare_transactions=0;
    uint32_t ext_address=0, ext_data=0, ext_mask=0, writeback_fault=UINT32_MAX;
    std::array<uint8_t,32> ext_line{};
    std::array<uint32_t,(REQUEST_BITS+31)/32> ext_held{};
    MemoryBench() {
        memory_service=system_service=trap_service=fault_support=true;
#ifdef ICACHE_CORE
        icache=true;
#endif
#ifdef DCACHE_CORE
        dcache=true; data_fatal_latency=2;
#endif
    }
    bool coherent() const { return dcache || target==architectural; }
    unsigned ext_status() const {
        if (ext_uncached) return ext_address==fault_address;
        return ext_write ? ext_address==writeback_fault:ext_address==(fault_address&~31U);
    }
    void drive_dcache() {
        if (!d.data_response_valid_i) return;
        put(d.data_response_i,RESPONSE_TRANSACTION_ID_OFFSET,4,ext_id);
        put(d.data_response_i,RESPONSE_STATUS_OFFSET,2,bad_response ? 2:ext_status());
        if (bad_response || ext_write || ext_status()) return;
        if (ext_uncached) {
            uint32_t word=0;
            for (unsigned n=0;n<(1U<<ext_size);n++) word|=uint32_t(target[ext_address+n])<<(8*((ext_address+n)%4));
            put(d.data_response_i,RESPONSE_UNCACHED_READ_DATA_OFFSET,32,word);
        } else for (unsigned n=0;n<32;n++) put(d.data_response_i,RESPONSE_LINE_READ_DATA_OFFSET+8*n,8,target[ext_address+n]);
    }
    void observe_dcache() {
        // a data-cache protocol fatal may withdraw an offered request.
        if (ext_offered && !bad_response && !d.memory_fatal_o) require(d.data_request_valid_o,"data request withdrawn");
        if (d.data_request_valid_o) {
            // hold the entire request through acceptance.
            if (ext_offered) for (unsigned n=0;n<REQUEST_BITS;n++)
                require(bit(d.data_request_o,n)==bit(ext_held,n),"data request changed under stall bit="+std::to_string(n));
            require(!accepted || answered,"overlapping data requests");
            const unsigned id=get32(d.data_request_o,REQUEST_TRANSACTION_ID_OFFSET,4);
            const bool write=bit(d.data_request_o,REQUEST_WRITE_OFFSET), uncached_request=bit(d.data_request_o,REQUEST_UNCACHED_OFFSET);
            const uint32_t a=get32(d.data_request_o,REQUEST_ADDRESS_OFFSET,32), line_mask=get32(d.data_request_o,REQUEST_LINE_WRITE_MASK_OFFSET,32);
            if (uncached_request) {
                require((prepared || canceled) && uncached && (!cause || address==fault_address),"unowned MMIO request");
                for (unsigned n=0;n<REQUEST_BITS;n++)
                    if (n<REQUEST_TRANSACTION_ID_OFFSET || n>=REQUEST_TRANSACTION_ID_OFFSET+4)
                        require(bit(d.data_request_o,n)==bit(packet,n),"MMIO request bit="+std::to_string(n));
            } else {
                require(a%32==0 && a<65536+0x10000 && (write ? line_mask==UINT32_MAX:line_mask==0),"D$ line request shape");
                for (unsigned n=0;n<32;n++) require(get32(d.data_request_o,REQUEST_UNCACHED_WRITE_DATA_OFFSET,32)==0
                    && get32(d.data_request_o,REQUEST_UNCACHED_WRITE_STROBE_OFFSET,4)==0 && get32(d.data_request_o,REQUEST_UNCACHED_SIZE_OFFSET,2)==0
                    && (write || get32(d.data_request_o,REQUEST_LINE_WRITE_DATA_OFFSET+8*n,8)==0),"D$ line request reserved fields");
            }
            offered=true;
            if (data_ready) {
                accepted=true; answered=ext_offered=false; remaining=data_latency; ++port_transactions;
                ext_id=id; ext_write=write; ext_uncached=uncached_request; ext_address=a;
                ext_size=get32(d.data_request_o,REQUEST_UNCACHED_SIZE_OFFSET,2);
                ext_data=get32(d.data_request_o,REQUEST_UNCACHED_WRITE_DATA_OFFSET,32);
                ext_strobe=get32(d.data_request_o,REQUEST_UNCACHED_WRITE_STROBE_OFFSET,4); ext_mask=line_mask;
                for (unsigned n=0;n<32;n++) ext_line[n]=uint8_t(get32(d.data_request_o,REQUEST_LINE_WRITE_DATA_OFFSET+8*n,8));
                coverage[uncached_request ? "dcache_mmio":write ? "dcache_writeback":"dcache_fill"]++;
                coverage["data_requests"]++;
            } else {
                ext_offered=true; coverage["data_request_stall"]++;
                coverage[uncached_request ? "dcache_mmio_stall":write ? "dcache_writeback_stall":"dcache_fill_stall"]++;
                for (unsigned n=0;n<ext_held.size();n++) ext_held[n]=d.data_request_o[n];
            }
        } else if (accepted && remaining) --remaining;
        if (!bad_response && d.data_response_valid_i && d.data_response_ready_o) {
            require(accepted && !answered,"unexpected data response");
            answered=true; coverage["data_responses"]++;
            if (!ext_status() && ext_write) {
                if (ext_uncached) { for (unsigned l=0;l<4;l++) if (ext_strobe>>l&1) target[(ext_address&~3U)+l]=uint8_t(ext_data>>(8*l)); }
                else for (unsigned n=0;n<32;n++) target[ext_address+n]=ext_line[n];
            }
            // writeback errors raise fatal on the next cycle.
            if (ext_write && !ext_uncached && ext_status()) { writeback_fatal_next=true; coverage["dcache_writeback_fault"]++; }
        }
    }
    // both ports share backing bytes; memory[] holds the architectural instruction oracle.
    uint32_t fetch_word(uint32_t a) const override {
        uint32_t word=0;
        for (unsigned n=0;n<4;n++) word|=uint32_t(target.at(a+n))<<(8*n);
        return word;
    }
    void drive_memory(const Input&) override {
        d.memory_issue_allowed_i=issue_allowed;
        d.admit_valid_i=admit && d.admission_valid_o;
        d.admit_id_i=d.admission_id_o ^ (wrong_admit ? 32:0);
        d.data_request_ready_i=data_ready;
        d.data_response_valid_i=bad_response || (accepted && !answered && !remaining);
        if (dcache) { drive_dcache(); return; }
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
        require(dcache ? !committed:target==architectural,"new memory owner before committed store drain");
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
        prepared=true; committed=offered=accepted=answered=canceled=false; prepare_transactions=port_transactions;
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
            prepared=offered=accepted=answered=committed=canceled=ext_offered=writeback_fatal_next=false; ids={}; allocations=watched_allocations=0;
            architectural.clear(); target.clear();
            for (unsigned n=0;n<65536;n++) architectural[n]=uint8_t(memory[n/4]>>(8*(n%4)));
            for (unsigned n=0;n<32;n++) architectural[0x10001000+n]=uint8_t(0x91+n);
            for (unsigned n=0;n<16;n++) architectural[0x10000000+n]=uint8_t(0xe1+n);
            target=architectural;
            require(!d.data_request_valid_o && !d.memory_prepare_o,"memory reset visibility");
            return;
        }
        if (writeback_fatal_next) { data_fatal=true; writeback_fatal_next=false; }
        if ((watch_older_fault || watch_branch_fence) && (d.dispatch_o&1) && uint32_t(d.fetch_pc_o)==watch_pc) {
            younger_dispatched=true; watched_allocations=allocations;
        }
        allocations+=(d.dispatch_o&1)+((d.dispatch_o>>1)&1);
        if (watch_older_fault && d.trap_accept_o) require(younger_dispatched,"older fault tested with younger memory dispatched");
        // a cache response releases the committed store, including on a hit.
        if (dcache && committed && !d.memory_committed_o) committed=false;
        require(dcache ? !d.memory_committed_o || committed:bool(d.memory_committed_o)==committed,"committed store ownership output");
        const bool barrier=pc<65536 && (fence(memory[pc/4]) || fence_i(memory[pc/4]));
        const std::string name=barrier && fence_i(memory[pc/4]) ? "fence_i_":"fence_";
        if (barrier && d.occupancy_o && !system_prepared && d.memory_busy_o) coverage[name+"order_wait"]++;
        if (d.system_prepare_o && barrier) {
            require(!prepared && !committed && (!accepted || answered) && coherent(),"FENCE prepared before older memory completion");
            coverage[name+"prepare"]++;
        }
        if (fatal && !offered && !committed) require(!d.data_request_valid_o,"new data request after frontend fatal");
        if (data_fatal && !held) require(!d.request_valid_o,"new instruction request after data fatal");
        // the data cache registers fatal for stray responses.
        if (bad_response && !dcache) require(!d.trap_accept_o,"trap accepted with malformed data response");
        require(bool(d.flush_ready_o)==!bool(d.memory_irrevocable_o),"flush permission");
        if (d.memory_prepare_o) prepare();
        if (prepared && !i.flush) require(!d.dispatch_o,"younger dispatch past memory barrier");
        if (d.admission_valid_o) {
            require(prepared && store && !uncached && !cause,"unexpected admission");
            uint32_t lanes=0; for (unsigned n=0;n<bytes;n++) lanes|=((data>>(8*n))&255)<<(8*((address+n)%4));
            require(d.admission_address_o==address && d.admission_data_o==lanes && d.admission_mask_o==mask && d.admission_size_o==kind%4,"admission descriptor");
            if (!admit || wrong_admit) { require(!d.retire_accept_o,"unadmitted store retirement"); coverage[wrong_admit ? "wrong_admission":"admission_stall"]++; }
        }
        // the affected instruction cannot complete while fatal propagates.
        if (data_fatal && !d.fatal_o) {
            require(!prepared || (!d.retire_accept_o && !d.trap_accept_o),"memory instruction completed inside fatal latency");
            coverage["dcache_fatal_latency"]++;
        }
        if (dcache) observe_dcache();
        else {
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
        if (e.op==34 || e.op==35) {
            require(!prepared && !committed && (!accepted || answered) && coherent(),"FENCE retired before older memory completion");
            return;
        }
        if (e.op!=32 && e.op!=33) return;
        require(prepared,"memory acceptance owner");
        if (store) {
            for (unsigned n=0;n<bytes;n++) {
                architectural[address+n]=uint8_t(data>>(8*n));
                if (address+n<65536) {
                    const unsigned shift=8*((address+n)%4);
                    memory[(address+n)/4]=(memory[(address+n)/4]&~(255U<<shift))|((data>>(8*n))&255)<<shift;
                }
            }
            if (uncached) require(answered && coherent(),"MMIO store completion before retirement");
            else { committed=true; coverage["store_commit"]++; }
        }
        if (dcache && !store && !uncached) coverage[port_transactions==prepare_transactions ? "dcache_load_hit":"dcache_load_miss"]++;
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
    b.memory.fill(instruction(2,0,0,0,0));
    for (unsigned n=0x4000/4;n<0x4100/4;n++) b.memory[n]=0x81fe80ffU+n;
    b.memory[0]=instruction(0,1,0,0,0x4000);
    b.memory[1]=instruction(0,2,0,0,0x87654000);
    b.memory[2]=instruction(2,2,2,0,0x321);
    b.memory[3]=instruction(0,3,0,0,0x10000000);
    b.watch_older_fault=b.watch_branch_fence=b.younger_dispatched=b.bad_response=false; b.watch_pc=20;
    b.issue_allowed=b.admit=b.data_ready=true; b.wrong_admit=false; b.fault_address=UINT32_MAX;
}
// hold the stop marker at the head until older work drains.
static Input hold_at(const MemoryBench& b,uint32_t stop,Input i) {
    if (b.pc==stop) i.retire=0;
    else if (b.pc+4==stop) i.retire&=1;
    return i;
}
static bool terminal(const MemoryBench& b,uint32_t stop) {
    return b.pc==stop && (b.d.retire_valid_o&1) && get32(b.d.retire_event_o,PC_BEFORE_OFFSET,32)==stop
        && !b.d.memory_busy_o && !b.d.fetch_busy_o && !b.d.system_busy_o;
}
template<class Stimulus>
static void run_until(MemoryBench& b,uint32_t stop,Stimulus stimulus) {
    for (unsigned n=0;n<15000;n++) {
        b.tick(hold_at(b,stop,stimulus(n)));
        b.require(!b.fatal,"unexpected fatal");
        if (terminal(b,stop)) {
            b.require(!b.prepared && !b.committed && !b.system_prepared && (!b.offered || b.answered)
                && b.coherent(),"terminal memory ownership or state mismatch");
            return;
        }
    }
    b.require(false,"memory program watchdog pc="+std::to_string(b.pc)
        +" memory_busy="+std::to_string(b.d.memory_busy_o)+" fetch_busy="+std::to_string(b.d.fetch_busy_o)
        +" occupancy="+std::to_string(b.d.occupancy_o)+" head="+std::to_string(b.d.retire_valid_o));
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
    initialize(b); b.memory[4]=mem(true,2,0,1,2); b.memory[5]=0x0ff0000f; b.reset();
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
            const auto ready=[&] { return b.pc==20 && (b.d.retire_valid_o&1) && !b.d.fetch_busy_o; };
            for (unsigned n=0;n<1000 && !ready();n++) b.tick(hold_at(b,20,Input{}));
            b.require(ready() && b.committed && b.d.memory_busy_o,"terminal guard setup");
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
            // cached stores receive guaranteed admission, so only loads may bus-fault there.
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
        // external cancellation drains offered reads and committed writes; reset clears the fabric too.
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
                run(b,0x808,rng); b.require(b.coherent(),"cancellation memory state");
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
        // a younger memory descriptor must not block the trap of an older fault.
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
        b.bad_response=true; { Input i; i.trap_ready=!b.dcache; b.tick(i); } b.bad_response=false; b.data_fatal=true;
        b.tick(); b.require(b.d.memory_fatal_o && b.d.memory_busy_o,"held fault frozen on protocol fatal");
        for (unsigned n=0;n<10;n++) { Input i; i.trap_ready=true; b.tick(i); b.require(!b.d.trap_accept_o && b.d.memory_busy_o,"held fault trapped after protocol fatal"); }
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
        // ordinary fence (kind 0) and fence.i (kind 1) share the head barrier; reserved fields are ignored.
        const std::array<std::array<uint32_t,3>,2> fences{{{0x0ff0000f,0xffff808f,0x8330000f},{0x0000100f,0xffff908f,0x8330100f}}};
        for (unsigned kind=0;kind<2;kind++) {
        const std::string key=kind ? "fence_i_":"fence_";
        // a barrier after each older memory class, under bus stalls and retire backpressure.
        for (unsigned test=0;test<12;test++) {
            initialize(b); unsigned at=4;
            if (test%4<3) b.memory[at++]=mem(test%4!=2,2,4,test%4==1 ? 3:1,2);
            b.memory[at++]=fences[kind][test%2];
            if (test>=8) b.memory[at++]=fences[kind][2];
            b.memory[at++]=csr(0xb02,2,0,6);
            b.reset(); b.data_ready=test<4; b.data_latency=60;
            const auto retired=b.coverage[key+"retired"];
            run_until(b,at*4,[&](unsigned n) {
                Input i; i.retire=test>=4 && test<8 && b.system_prepared ? n%3==0:3;
                b.data_ready=test<4 || n%5==0; return i;
            });
            b.require(b.regs[6]==4+(test%4<3)+1+(test>=8)
                && b.coverage[key+"retired"]==retired+1+(test>=8),"FENCE retirement count");
            b.coverage[key+(test%4==3 ? "empty":test%4==2 ? "after_load":test%4==1 ? "after_mmio":"after_store")]++;
            if (test>=8) b.coverage[key+"back_to_back"]++;
        }
        // flush or reset while the barrier waits for order or is held at retirement; older faults beat it.
        for (unsigned held=0;held<2;held++) for (unsigned cancel=0;cancel<3;cancel++) {
            initialize(b); b.memory[4]=mem(true,2,0,1,2); b.memory[5]=fences[kind][0];
            b.memory[0x800/4]=fences[kind][0]; b.memory[0x804/4]=csr(0xb02,2,0,6);
            b.reset(); b.data_latency=60;
            unsigned reached=0;
            for (unsigned n=0;n<2000 && reached<4;n++) {
                Input i; i.retire=held && b.system_prepared ? 0:3; b.tick(i);
                if (held ? b.system_prepared:b.pc==20 && b.committed && b.d.occupancy_o && b.d.memory_busy_o && !b.system_prepared) reached++;
            }
            b.require(reached==4,"FENCE cancellation watchdog");
            const auto redirects=b.coverage["fence_i_redirect"];
            if (cancel==1) b.reset();
            else if (cancel==0) { Input i; i.flush=true; i.target=0x800; b.tick(i); run(b,0x808,rng); }
            else {
                b.bad_response=true; { Input i; i.retire=0; b.tick(i); } b.bad_response=false; b.data_fatal=true;
                const auto retired=b.retired;
                for (unsigned n=0;n<40;n++) { Input i; i.retire=3; b.tick(i); }
                b.require(b.retired==retired && b.d.memory_fatal_o && !b.d.retire_accept_o,"FENCE retired after data fatal");
            }
            b.require(b.coverage["fence_i_redirect"]==redirects+(kind && !cancel),"FENCE.I redirect count after cancellation");
            b.coverage[key+(held ? "held_":"wait_")+(cancel==1 ? "reset":cancel ? "fatal":"flush")]++;
        }
        initialize(b); b.memory[4]=mem(true,2,0,1,2); b.memory[5]=0xffffffff; b.memory[6]=fences[kind][0];
        b.memory[0x100/4]=csr(0x342,2,0,10); b.reset(); b.watch_older_fault=true; b.watch_pc=24; b.data_latency=60;
        for (unsigned n=0;n<1000 && !b.younger_dispatched;n++) { b.data_ready=false; b.tick(); }
        b.require(b.younger_dispatched && b.committed,"younger FENCE allocation watchdog"); run(b,0x104,rng);
        b.require(b.regs[10]==2,"older fault behind FENCE"); b.coverage["older_fault_"+key.substr(0,key.size()-1)]++;
        // hold an older branch until the wrong-path barrier owns the next rob slot.
        for (unsigned pad:{0u,27u}) {
            initialize(b); unsigned at=4;
            for (unsigned n=0;n<pad;n++) b.memory[at++]=instruction(2,0,0,0,0);
            const unsigned branch_pc=at*4;
            b.memory[at++]=instruction(21,0,0,0,16);
            b.memory[at++]=fences[kind][0];
            b.memory[at++]=instruction(2,8,0,0,0x66); b.memory[at++]=instruction(2,8,0,0,0x77);
            b.memory[at++]=fences[kind][2];
            b.memory[at++]=instruction(2,8,0,0,0x55); b.memory[at++]=csr(0xb02,2,0,6);
            const auto retired=b.retired, prepares=b.coverage[key+"prepare"], fences_retired=b.coverage[key+"retired"];
            const auto redirects=b.coverage["branch_redirect"], refetches=b.coverage["fence_i_redirect"];
            b.reset(); b.watch_branch_fence=true; b.watch_pc=branch_pc+4;
            Input held; held.grant=false;
            for (unsigned n=0;n<1000 && !(b.younger_dispatched && b.pc==branch_pc && b.d.occupancy_o==2);n++) b.tick(held);
            b.require(b.younger_dispatched && b.watched_allocations==pad+5 && b.allocations==pad+6
                && b.pc==branch_pc && b.d.occupancy_o==2,"wrong-path FENCE allocation setup");
            for (unsigned n=0;n<20;n++) {
                b.tick(held);
                b.require(b.pc==branch_pc && b.d.occupancy_o==2 && b.allocations==pad+6 && !b.system_prepared
                    && b.coverage[key+"prepare"]==prepares && b.coverage[key+"retired"]==fences_retired,"wrong-path FENCE held barrier");
            }
            for (unsigned n=0;n<100 && b.coverage["branch_redirect"]==redirects;n++) b.tick();
            b.require(b.coverage["branch_redirect"]==redirects+1,"FENCE branch recovery watchdog");
            run(b,at*4,rng);
            b.require(b.coverage[key+"prepare"]==prepares+1 && b.coverage[key+"retired"]==fences_retired+1
                && b.coverage["branch_redirect"]==redirects+1 && b.coverage["fence_i_redirect"]==refetches+kind
                && b.regs[8]==0x55 && b.regs[6]==pad+7 && b.retired==retired+pad+8,"FENCE squash, reuse and retirement count");
            b.coverage[key+(pad ? "branch_squash_wrap":"branch_squash")]++;
        }
        }
        // fence.i must discard prefetched bytes after a byte, halfword or word code patch.
        const uint32_t old_word=instruction(2,8,0,0,0x111);
        for (unsigned test=0;test<48;test++) {
            const unsigned width=test%3, slot=(test/3)%8, style=test/24;
            // replacement bytes keep a valid addi x8 whose source stays x0.
            const uint32_t value=std::array<uint32_t,3>{0xa5,0xa5c0,instruction(2,8,0,0,0x5a5)}[width];
            const unsigned offset=4-(1U<<width), shift=8*offset;
            const uint32_t mask=width==2 ? UINT32_MAX:((1U<<(8U<<width))-1)<<shift;
            const uint32_t new_word=(old_word&~mask)|((value<<shift)&mask);
            initialize(b); unsigned at=4;
            b.memory[at++]=instruction(0,9,0,0,(value+0x800)&0xfffff000U);
            b.memory[at++]=instruction(2,9,9,0,value&4095);
            while (at<7 || (at+1)%8!=slot) b.memory[at++]=instruction(2,0,0,0,0);
            const unsigned replaced=at+2+style;
            b.memory[at++]=mem(true,width,0,0,9,int(replaced*4+offset));
            b.memory[at++]=0x0000100f;
            if (style) b.memory[at++]=0xffff908f;
            b.memory[at++]=old_word;
            b.memory[at++]=csr(0xb02,2,0,6);
            b.reset();
            const auto stale=b.coverage["stale_fetch_visible"], refetches=b.coverage["fence_i_redirect"];
            const auto fills=b.line_fills[replaced*4&~31U];
            run_until(b,at*4,[&](unsigned n) {
                Input i; i.retire=style && b.system_prepared ? n%3==0:rng()%4; i.request_ready=rng()%3!=0; i.latency=rng()%5;
                b.data_ready=style ? n%5==0:rng()%3!=0; b.data_latency=style ? 40:rng()%9;
                return i;
            });
            b.require(b.memory[replaced]==new_word && b.regs[8]==extend(new_word>>20,12) && b.regs[6]==replaced+1,"self-modified instruction result");
            b.require(b.coverage["fence_i_redirect"]==refetches+1+style,"FENCE.I refetch count");
            if (b.coverage["stale_fetch_visible"]>stale) b.coverage["smc_stale_buffered"]++;
            // the replaced line was resident; after fence.i it must come from the port again.
            if (b.icache) { b.require(b.line_fills[replaced*4&~31U]>=fills+2,"FENCE.I refill of resident line"); b.coverage["icache_smc_refill"]++; }
            if (b.dcache) { b.require(b.fetch_word(replaced*4)==new_word,"FENCE.I cleaned the D$ store to memory"); b.coverage["dcache_smc_clean"]++; }
            b.coverage["smc_width_"+std::to_string(width)]++;
        }
        // fence.i ends a line, so the next line's old fetch request crosses the barrier.
        for (unsigned mode=0;mode<3;mode++) {
            initialize(b);
            const uint32_t value=instruction(2,8,0,0,0x2c3);
            b.memory[4]=instruction(0,9,0,0,(value+0x800)&0xfffff000U); b.memory[5]=instruction(2,9,9,0,value&4095);
            b.memory[6]=mem(true,2,0,0,9,32); b.memory[7]=0x0000100f;
            b.memory[8]=old_word; b.memory[9]=csr(0xb02,2,0,6);
            b.reset(); b.data_ready=false;
            const auto request=[&] { return b.d.request_valid_o && get32(b.d.request_o,REQUEST_ADDRESS_OFFSET,32)==32; };
            bool reached=false;
            for (unsigned n=0;n<1000 && !reached;n++) {
                Input i; i.request_ready=!(mode==0 && request()); i.latency=request() ? 1000:1; b.tick(i);
                reached=b.committed && (mode==0 ? b.held:b.pending && b.pending_address==32);
            }
            b.require(reached && (mode==0 || b.pending_line[0]==old_word),"old FENCE.I line request setup");
            b.data_ready=true;
            for (unsigned n=0;n<1000 && !(b.system_prepared && (b.d.retire_valid_o&1));n++) { Input i; i.retire=0; i.request_ready=mode!=0; b.tick(i); }
            b.require(b.system_prepared && (b.d.retire_valid_o&1) && !b.committed && (mode==0 ? b.held:b.pending),"held FENCE.I with old fetch traffic");
            const auto stale=b.coverage["stale_response"], pending=b.coverage["redirect_pending"], stalled=b.coverage["redirect_request_stall"];
            const auto refetches=b.coverage["fence_i_redirect"];
            if (mode==2) b.delay=0;
            { Input i; i.request_ready=false; b.tick(i); }
            b.require(b.coverage["fence_i_redirect"]==refetches+1 && (mode==0 ? b.coverage["redirect_request_stall"]==stalled+1:b.coverage["redirect_pending"]==pending+1),"FENCE.I redirect over old fetch traffic");
            if (mode==2) b.require(!b.pending && b.coverage["stale_response"]==stale+1,"response coincident with FENCE.I redirect");
            b.delay=0;
            run(b,40,rng);
            // discard stale fetch data even when its request is accepted before the clean.
            b.require(b.regs[8]==0x2c3 && b.coverage["stale_response"]>=stale+(mode!=0) && b.coverage["stale_response"]<=stale+(mode!=0 || b.dcache),"stale FENCE.I response discarded");
            b.coverage[std::array<const char*,3>{"fence_i_held_request","fence_i_delayed_response","fence_i_coincident_response"}[mode]]++;
        }
#ifdef ICACHE_CORE
        // a loop inside one line refills it once; every later iteration hits.
        for (unsigned trip:{2u,9u}) {
            initialize(b); b.memory[4]=instruction(2,5,0,0,int(trip));
            b.memory[5]=instruction(2,5,5,0,-1); b.memory[6]=instruction(22,0,5,0,-4);
            b.memory[7]=csr(0xb02,2,0,6);
            const auto fills=b.line_fills[0], hits=b.coverage["icache_hit"];
            b.reset(); run(b,32,rng);
            b.require(b.line_fills[0]==fills+1 && b.regs[5]==0 && b.coverage["icache_hit"]>=hits+trip-1,"I$ loop hits");
            b.coverage["icache_loop"]++;
        }
        // a failed refill traps and installs nothing: the retry after the handler refills again.
        for (bool slow:{false,true}) {
            initialize(b);
            b.memory[4]=instruction(2,7,0,0,0x400); b.memory[5]=csr(0x305,1,7,0);
            b.memory[6]=instruction(27,0,0,0,0x40-24);
            b.memory[0x40/4]=instruction(2,8,0,0,0x77); b.memory[0x44/4]=csr(0xb02,2,0,6);
            b.memory[0x400/4]=csr(0x342,2,0,10); b.memory[0x404/4]=instruction(27,0,0,0,0x40-0x404);
            b.error_address=0x40;
            const auto fills=b.line_fills[0x40], traps=b.traps;
            b.reset();
            run_until(b,0x48,[&](unsigned) {
                if (b.traps>traps) b.error_address=UINT32_MAX;
                Input i; i.trap_ready=rng()%3!=0; i.request_ready=!slow || rng()%4==0; i.latency=slow ? 8:rng()%5;
                return i;
            });
            b.require(b.traps==traps+1 && b.regs[10]==1 && b.regs[8]==0x77 && b.line_fills[0x40]==fills+1,"I$ refill fault retry");
            b.coverage[slow ? "icache_fault_slow":"icache_fault"]++;
        }
#endif
#ifdef DCACHE_CORE
        // verify dirty eviction after store misses and stores to clean hits.
        for (unsigned order:{0u,1u}) for (unsigned slow:{0u,1u}) {
            initialize(b); unsigned at=4;
            for (unsigned k=0;k<4;k++) {
                if (order) b.memory[at++]=mem(false,2,20+k,1,0,int(k*0x200+4*k));
                b.memory[at++]=mem(true,2,0,1,2,int(k*0x200+4*k)); b.memory[at++]=instruction(2,2,2,0,1);
            }
            for (unsigned k=0;k<4;k++) b.memory[at++]=mem(false,2,10+k,1,0,int(k*0x200+4*k));
            b.memory[at++]=0x0000100f; b.memory[at++]=csr(0xb02,2,0,6);
            const auto writebacks=b.coverage["dcache_writeback"];
            b.reset();
            run_until(b,at*4,[&](unsigned) {
                Input i; i.retire=rng()%4; i.request_ready=rng()%3!=0; i.latency=rng()%5;
                b.data_ready=!slow || rng()%4==0; b.data_latency=slow ? 12:rng()%5; return i;
            });
            for (unsigned k=0;k<4;k++) b.require(b.regs[10+k]==0x87654321U+k,"D$ evicted store read back");
            b.require(b.coverage["dcache_writeback"]>=writebacks+2 && b.target==b.architectural,"D$ writeback and FENCE.I clean");
            b.coverage["dcache_conflict"]++;
        }
        // clean both ways in sets 0, 7 and 15 without prior eviction.
        for (unsigned slow:{0u,1u}) {
            initialize(b); unsigned at=4;
            for (unsigned set:{0u,7u,15u}) for (unsigned way:{0u,1u}) {
                b.memory[at++]=mem(true,2,0,1,2,int(way*0x200+set*32+4*way));
                b.memory[at++]=instruction(2,2,2,0,1);
            }
            b.memory[at++]=0x0000100f; b.memory[at++]=csr(0xb02,2,0,6);
            const auto writebacks=b.coverage["dcache_writeback"];
            b.reset();
            run_until(b,at*4,[&](unsigned) {
                Input i; i.retire=rng()%4; i.request_ready=rng()%3!=0; i.latency=rng()%5;
                b.data_ready=!slow || rng()%4==0; b.data_latency=slow ? 12:rng()%5; return i;
            });
            b.require(b.coverage["dcache_writeback"]==writebacks+6 && b.target==b.architectural,"FENCE.I cleans both ways of every set");
            b.coverage["dcache_clean_all_ways"]++;
        }
        // a data-cache fatal must suppress pending recycle restart and drain effects.
        {
            initialize(b);
            b.memory[4]=instruction(2,5,0,0,2047);
            b.memory[8]=instruction(2,5,5,0,-1); b.memory[9]=instruction(22,0,5,0,-16);
            b.memory[10]=csr(0xb02,2,0,6);
            b.reset();
            for (unsigned n=0;n<200000 && !b.d.identity_drain_o;n++) b.tick();
            b.require(b.d.identity_drain_o,"identity recycle setup");
            for (unsigned n=0;n<1000 && (b.d.occupancy_o || b.d.fetch_busy_o || b.d.memory_busy_o || b.d.system_busy_o);n++) b.tick();
            b.require(b.d.identity_drain_o && !b.d.occupancy_o && !b.d.fetch_busy_o,"identity recycle quiet setup");
            const auto retired=b.retired;
            b.bad_response=true; b.tick(); b.bad_response=false; b.data_fatal=true;
            for (unsigned n=0;n<40;n++) { b.tick(); b.require(!b.d.redirect_o,"recycle restart after D$ fatal"); }
            b.require(b.d.fatal_o && b.d.identity_drain_o && b.retired==retired,"D$ fatal freezes identity recycling");
            b.coverage["dcache_fatal_recycle"]++;
        }
        // failed writeback is fatal because the store has already retired.
        {
            initialize(b); unsigned at=4;
            for (unsigned k=0;k<3;k++) b.memory[at++]=mem(true,2,0,1,2,int(k*0x200));
            b.memory[at++]=csr(0xb02,2,0,6);
            b.writeback_fault=0x4000; b.reset();
            for (unsigned n=0;n<2000 && !b.data_fatal;n++) b.tick();
            b.require(b.data_fatal,"D$ writeback fault watchdog");
            const auto retired=b.retired;
            for (unsigned n=0;n<40;n++) b.tick();
            b.require(b.d.fatal_o && b.d.memory_fatal_o && !b.d.data_request_valid_o && b.retired==retired,"D$ writeback fault freezes the core");
            b.writeback_fault=UINT32_MAX; b.coverage["dcache_fatal"]++;
        }
        // an mmio protocol error must not complete the access while fatal propagates.
        for (bool store:{false,true}) {
            initialize(b); b.memory[4]=mem(store,2,4,3,2); b.reset(); b.data_latency=30;
            for (unsigned n=0;n<1000 && !(b.accepted && !b.answered && b.ext_uncached);n++) { Input i; i.retire=b.prepared && !store ? 0:3; b.tick(i); }
            b.require(b.accepted && !b.answered && b.ext_uncached,"MMIO protocol fatal setup");
            const auto retired=b.retired;
            b.bad_response=true; { Input i; i.trap_ready=true; b.tick(i); } b.bad_response=false; b.data_fatal=true;
            for (unsigned n=0;n<40;n++) { Input i; i.trap_ready=true; b.tick(i); }
            b.require(b.d.memory_fatal_o && b.retired==retired && !b.d.retire_accept_o,"MMIO protocol error never completes");
            b.coverage[store ? "dcache_mmio_store_protocol_fatal":"dcache_mmio_load_protocol_fatal"]++;
        }
#endif
        for (unsigned round=0;round<20;round++) {
            initialize(b); unsigned at=4;
            for (unsigned n=0;n<100;n++) {
                bool store=rng()%2; unsigned kind=store ? rng()%3:std::array<unsigned,5>{0,1,2,4,5}[rng()%5];
                unsigned offset=(rng()%16)&~((1u<<(kind%4))-1);
                b.memory[at++]=mem(store,kind,4,1,2,offset);
                b.memory[at++]=instruction(2,2,2,0,int(rng()%31)-15);
                if (n%7==0) { b.memory[at++]=instruction(27,0,0,0,8); b.memory[at++]=mem(true,2,0,3,2); }
                if (rng()%4==0) b.memory[at++]=std::array<uint32_t,6>{0x0ff0000f,0x0000000f,0xffff808f,0x8330000f,0x0000100f,0xffff908f}[rng()%6];
            }
            b.memory[at++]=csr(0xb02,2,0,6);
            b.reset(); run(b,at*4,rng); b.coverage["random_program"]++;
        }
        std::cout<<"MEMORY CORE PASS seed="<<seed<<" cycles="<<b.cycles<<" retired="<<b.retired<<" traps="<<b.traps;
        for (const auto& [name,count]:b.coverage) std::cout<<" "<<name<<"="<<count;
        std::cout<<"\n";
    } catch(const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
