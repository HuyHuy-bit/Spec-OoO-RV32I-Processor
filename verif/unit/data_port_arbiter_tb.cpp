#include "Vdata_port_arbiter.h"
#include "verilated.h"
#include "packed_bits.hpp"
#include "fetch_memory_layout.hpp"
#include <algorithm>
#include <array>
#include <bitset>
#include <cstdint>
#include <iostream>
#include <map>
#include <random>
#include <stdexcept>
#include <string>
using packed_bits::put;
using Request=std::array<uint32_t,(REQUEST_BITS+31)/32>;
using Response=std::array<uint32_t,(RESPONSE_BITS+31)/32>;
struct Input {
    bool reset=false, request_ready=false, response_valid=false;
    unsigned reserve=0, fatal=0, request_valid=2, response_ready=0;
    std::array<Request,3> requests{};
    Response response{};
};
struct Bench {
    Vdata_port_arbiter d;
    std::mt19937 rng;
    std::array<unsigned,3> priority{{0,1,2}};
    int owner=-1;
    bool sent=false, request_held=false, response_held=false;
    Request held_request{};
    Response held_response{};
    unsigned fatal=0, fatal_clients=0, cycles=0, cases=0;
    std::map<std::string,unsigned> count;
    std::array<std::bitset<REQUEST_BITS>,3> request_bits;
    std::array<std::bitset<RESPONSE_BITS>,3> response_bits;
    explicit Bench(unsigned seed):rng(seed) {}
    void require(bool ok,const std::string& why) const {
        if (!ok) throw std::runtime_error("data port mismatch cycle="+std::to_string(cycles)+": "+why);
    }
    template<class A,class B> bool equal(const A& a,const B& b,unsigned bits) const {
        for (unsigned n=0;n<bits;++n) if (packed_bits::bit(a,n)!=packed_bits::bit(b,n)) return false;
        return true;
    }
    Input input(unsigned id=0) {
        Input i;
        for (auto& p:i.requests) { for (auto& word:p) word=rng(); put(p,REQUEST_TRANSACTION_ID_OFFSET,4,id); }
        for (auto& word:i.response) word=rng();
        put(i.response,RESPONSE_TRANSACTION_ID_OFFSET,4,id);
        return i;
    }
    void drive(const Input& i) {
        d.clk_i=0; d.rst_i=i.reset; d.reserve_i=i.reserve; d.client_fatal_i=i.fatal;
        d.client_request_valid_i=i.request_valid; d.client_response_ready_i=i.response_ready;
        d.request_ready_i=i.request_ready; d.response_valid_i=i.response_valid;
        for (unsigned n=0;n<(3*REQUEST_BITS+31)/32;++n) d.client_request_i[n]=0;
        for (unsigned c=0;c<3;++c) for (unsigned n=0;n<REQUEST_BITS;++n)
            put(d.client_request_i,c*REQUEST_BITS+n,1,packed_bits::bit(i.requests[c],n));
        for (unsigned n=0;n<i.response.size();++n) d.response_i[n]=i.response[n];
    }
    void step(Input i) {
        drive(i); d.eval();
        bool live=!i.reset && !fatal && !i.fatal;
        bool unexpected=i.response_valid && (owner<0 || !sent);
        unsigned grant=0, owned=owner<0 ? 0:1U<<owner;
        if (live && !unexpected && owner<0) for (unsigned c:priority) if ((i.reserve>>c)&1U) { grant=1U<<c; break; }
        bool offer=live && owner>=0 && !sent && (i.request_valid&owned);
        unsigned request_ready=live && owner>=0 && !sent && i.request_ready ? owned:0;
        unsigned response_valid=live && sent && i.response_valid ? owned:0;
        bool response_ready=live && sent && (i.response_ready&owned);
        require(d.grant_o==grant,"round-robin reservation");
        require(bool(d.busy_o)==(!i.reset && owner>=0) && d.owner_o==(i.reset ? 0:owned),"port ownership");
        require(bool(d.request_valid_o)==offer && d.client_request_ready_o==request_ready,"request handshake routing");
        require(d.client_response_valid_o==response_valid && bool(d.response_ready_o)==response_ready,"response owner routing");
        Request packet{}; if (offer) packet=i.requests.at(owner);
        require(equal(d.request_o,packet,REQUEST_BITS),"request packet selection");
        require(equal(d.client_response_o,i.response,RESPONSE_BITS),"broadcast response transparency");
        if (!i.reset) require(bool(d.fatal_o)==bool(fatal) && d.fatal_reason_o==fatal
            && d.fatal_clients_o==fatal_clients,"first fatal diagnostics");
        if (live && request_held) require(offer && equal(d.request_o,held_request,REQUEST_BITS),"held target request");
        if (live && response_held) require(i.response_valid && equal(i.response,held_response,RESPONSE_BITS),"target response stimulus hold");
        request_held=offer && !i.request_ready; held_request=packet;
        response_held=live && sent && i.response_valid && !response_ready; held_response=i.response;
        bool request_fire=offer && i.request_ready, response_fire=i.response_valid && response_ready;
        unsigned error=i.fatal ? 2:unexpected ? 1:0;
        d.clk_i=1; d.eval(); ++cycles;
        if (i.reset) {
            owner=-1; sent=request_held=response_held=false; fatal=fatal_clients=0; priority={{0,1,2}}; ++count["reset"];
        } else {
            if (request_fire) ++count["requests"];
            if (response_fire) ++count["responses"];
            if (!fatal) {
                if (error) { fatal=error; fatal_clients=i.fatal; ++count["fatal_"+std::to_string(error)]; }
                else if (grant) {
                    for (unsigned c=0;c<3;++c) if (grant==(1U<<c)) owner=c;
                    auto selected=std::find(priority.begin(),priority.end(),unsigned(owner));
                    std::rotate(priority.begin(),selected+1,priority.end());
                    sent=false; ++cases; ++count["grant_"+std::to_string(owner)];
                } else if (request_fire) sent=true;
                else if (response_fire) { owner=-1; sent=false; }
            }
        }
    }
    void reset() { auto i=input(); i.reset=true; step(i); step(input()); }
    unsigned transact(unsigned contenders,bool fast,int request_bit=-1,int response_bit=-1,unsigned id=0) {
        auto i=input(id); i.reserve=contenders;
        if (request_bit>=0) for (unsigned c=0;c<3;++c) if (contenders==(1U<<c)) {
            i.requests[c]={}; put(i.requests[c],request_bit,1,1); request_bits[c].set(request_bit);
        }
        if (response_bit>=0) { i.response={}; put(i.response,response_bit,1,1); }
        step(i); require(owner>=0,"grant required"); unsigned c=owner, mask=1U<<c;
        ++count["contenders_"+std::to_string(contenders)];
        if (response_bit>=0) response_bits[c].set(response_bit);
        i.reserve=rng()%8; i.request_valid=2|mask;
        unsigned stalls=fast ? 0:1+rng()%4;
        for (unsigned n=0;n<stalls;++n) { step(i); ++count["request_stall"]; }
        i.request_ready=true; step(i); i.request_valid=c==1 ? 0:2;
        for (unsigned n=0;n<stalls;++n) { i.response_ready=rng()%8; step(i); ++count["response_wait"]; }
        i.response_valid=true; i.response_ready=7^mask;
        for (unsigned n=0;n<stalls;++n) { step(i); ++count["response_stall"]; }
        i.response_ready=mask|(rng()%8); step(i);
        ++count[fast ? "fast":"delayed"];
        if (request_bit<0 && response_bit<0) ++count["aliased_ids"];
        return c;
    }
    Input acquire(unsigned c) {
        auto i=input(); i.reserve=1U<<c; step(i); i.request_valid=2|(1U<<c); i.reserve=7; return i;
    }
    void freeze(Input i) {
        require(fatal!=0,"fatal expected");
        i.reserve=7; i.fatal=0; i.response_valid=false; i.request_ready=true; i.response_ready=7;
        step(i); ++count["fatal_quiet"];
        for (unsigned source:{0U,7U}) for (bool response:{false,true}) {
            i.fatal=source; i.response_valid=response; step(i); ++count["fatal_hold"];
            ++count["fatal_inputs_"+std::to_string(source)+"_"+std::to_string(response)];
        }
        reset();
    }
    void unexpected(unsigned kind) {
        reset(); auto i=input();
        if (kind) i=acquire(kind%3);
        if (kind==3) {
            i.request_ready=true; step(i); i.request_valid=0;
            i.response_valid=true; i.response_ready=7; step(i);
        }
        i.reserve=7; i.response_valid=true; i.request_ready=kind==2;
        step(i); ++count["unexpected_"+std::to_string(kind)]; freeze(i);
    }
    void client_fault(unsigned c,unsigned phase) {
        reset(); auto i=input();
        if (phase) i=acquire(c);
        if (phase>=2) { i.request_ready=true; step(i); i.request_valid=c==1 ? 0:2; }
        if (phase==3) {
            put(i.response,RESPONSE_STATUS_OFFSET,2,2);
            i.response_valid=true; i.response_ready=7; step(i);
            i.response_valid=false; ++count["registered_fatal"];
        }
        i.reserve=7; i.fatal=1U<<c; i.request_ready=true;
        if (phase==2) { i.response_valid=true; i.response_ready=7; }
        step(i); ++count["client_"+std::to_string(c)+"_phase_"+std::to_string(phase)]; freeze(i);
    }
    void resets() {
        for (unsigned c=0;c<3;++c) for (unsigned phase=0;phase<3;++phase) {
            reset(); auto i=input();
            if (phase) i=acquire(c);
            if (phase==2) { i.request_ready=true; step(i); i.request_valid=c==1 ? 0:2; }
            i.reset=true; i.reserve=7; i.request_ready=true; i.response_valid=phase==2; i.response_ready=7;
            step(i); step(input()); ++count["reset_phase_"+std::to_string(phase)];
            require(transact(7,true)==0,"reset round-robin priority"); ++count["reset_rr"];
        }
    }
    void negative(const std::string& name) {
        reset(); auto i=input();
        if (name=="load-unowned") i.request_valid=3;
        else if (name=="mmio-unowned") i.request_valid=6;
        else if (name=="missing" || name=="withdraw") {
            i=acquire(0); if (name=="withdraw") step(i); i.request_valid=2;
        } else if (name=="wrong-client") { i=acquire(0); i.request_valid=7; }
        else throw std::runtime_error("unknown negative");
        drive(i); d.eval(); d.clk_i=1; d.eval(); throw std::runtime_error("caller violation survived");
    }
};
int main(int argc,char** argv) {
    Verilated::commandArgs(argc,argv);
    try {
        if (argc==3 && std::string(argv[1])=="negative") { Bench b(1); b.negative(argv[2]); return 1; }
        unsigned seed=argc>1 ? std::stoul(argv[1]):1, random=argc>2 ? std::stoul(argv[2]):2000;
        Bench b(seed); b.reset();
        for (unsigned mask=1;mask<8;++mask) { b.transact(mask,true); b.transact(mask,false); }
        for (unsigned n=0;n<60;++n) ++b.count["fair_"+std::to_string(b.transact(7,true))];
        for (unsigned c=0;c<3;++c) {
            b.require(b.count["fair_"+std::to_string(c)]==20,"saturated fairness");
            for (unsigned id=0;id<16;++id) b.transact(1U<<c,true,-1,-1,id);
            for (unsigned bit=0;bit<REQUEST_BITS;++bit) b.transact(1U<<c,true,bit);
            for (unsigned bit=0;bit<RESPONSE_BITS;++bit) b.transact(1U<<c,true,-1,bit);
        }
        for (unsigned kind=0;kind<4;++kind) b.unexpected(kind);
        for (unsigned c=0;c<3;++c) for (unsigned phase=0;phase<4;++phase) b.client_fault(c,phase);
        for (unsigned mask:{3U,5U,6U,7U}) {
            auto i=b.input(); i.fatal=mask; i.reserve=7; i.response_valid=true;
            b.step(i); ++b.count["fatal_mask_"+std::to_string(mask)]; b.freeze(i);
        }
        b.resets();
        for (unsigned n=0;n<random;++n) b.transact(1+b.rng()%7,b.rng()%2,-1,-1,b.rng()%16);
        b.step(b.input()); b.require(b.owner<0 && !b.fatal,"final drain");
        for (unsigned c=0;c<3;++c) {
            b.count["request_bits_"+std::to_string(c)]=b.request_bits[c].count();
            b.count["response_bits_"+std::to_string(c)]=b.response_bits[c].count();
        }
        std::cout<<"DATA PORT PASS seed="<<seed<<" cycles="<<b.cycles<<" cases="<<b.cases<<" random="<<random;
        for (const auto& c:b.count) std::cout<<" "<<c.first<<"="<<c.second;
        std::cout<<"\n";
    } catch (const std::exception& e) { std::cerr<<e.what()<<"\n"; return 1; }
}
