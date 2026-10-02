import copy
import unittest

from model.iss.sail_log import parse_sail_log, SailLogError
from model.iss.spike_log import relocate, spike_sail_trace, SpikeLogError
from tools.run_sail_differential import packets
from verif.core.programs import i, store, branch, csr, constant
from verif.lockstep.comparator import compare_traces, ComparisonError


def frame(order, pc, ins, effects=''):
    return f'[{order}] [M]: 0x{pc:08X} (0x{ins:08X}) instruction\n{effects}\n\n'


MEMORY = (frame(0,0,i(1,0,128),'x1 <- 0x00000080')
          + frame(1,4,i(2,1,1,0,3),'mem[R,0x000000081] -> 0xFF\nx2 <- 0xFFFFFFFF')
          + frame(2,8,store(1,2,2,0),'mem[W,0x000000082] <- 0xFF')
          + frame(3,12,branch(2,2,8)) + frame(4,20,i(0,0,0)))
TRAP_EFFECTS = ('trapping from M to M to handle environment-call-from-M-mode\n'
                'CSR mstatus (0x300) <- 0x00001800\nCSR mstatush (0x310) <- 0x00000000\n'
                'CSR mcause (0x342) <- 0x0000000B\nCSR mtval (0x343) <- 0x00000000\n'
                'CSR mepc (0x341) <- 0x00000000')
TRAP = (frame(0,0,0x73,TRAP_EFFECTS)
        + frame(1,256,0x30200073,'CSR mstatus (0x300) <- 0x00001880\nCSR mstatush (0x310) <- 0x00000000\nret-ing from M to M')
        + frame(2,0,i(0,0,0)))
CSR_TRACE = (frame(0,0,i(1,0,7),'x1 <- 0x00000007')
             + frame(1,4,csr(2,1,0x340,1),'CSR mscratch (0x340) <- 0x00000007\nx2 <- 0x00000000')
             + frame(2,8,csr(3,0,0x340,2),'CSR mscratch (0x340) -> 0x00000007\nx3 <- 0x00000007')
             + frame(3,12,i(0,0,0)))


class SailLogTest(unittest.TestCase):
    def test_memory_lanes_sources_and_observed_next_pc(self):
        events = parse_sail_log(MEMORY)
        self.assertEqual(events[1]['mem_read_mask'],2)
        self.assertEqual(events[1]['mem_read_data'],0xff00)
        self.assertEqual(events[1]['rd_value'],0xffffffff)
        self.assertEqual(events[2]['mem_write_mask'],4)
        self.assertEqual(events[2]['mem_write_data'],0xff0000)
        self.assertEqual(events[3]['rs1_value'],0xffffffff)
        self.assertEqual(events[3]['pc_after'],20)

    def test_trap_and_mret_effect_order(self):
        events = parse_sail_log(TRAP)
        self.assertEqual(events[0]['trap_cause'],11)
        self.assertEqual(events[0]['retired'],0)
        self.assertEqual([e['address'] for e in events[0]['csr_effects']],[0x300,0x341,0x342,0x343])
        self.assertEqual(events[1]['csr_effects'][0]['old_value'],0x1800)
        self.assertEqual(events[1]['csr_effects'][0]['mask_reason'],3)

    def test_csr_observations_and_read_only_access(self):
        events = parse_sail_log(CSR_TRACE)
        self.assertEqual(events[1]['csr_effects'][0]['new_value'],7)
        self.assertEqual(events[2]['csr_effects'][0]['write_mask'],0)

    def test_fetch_fault_has_its_own_boundary(self):
        fault = TRAP_EFFECTS.replace('environment-call-from-M-mode','fetch-access-fault').replace('0x0000000B','0x00000001').replace('CSR mepc (0x341) <- 0x00000000','CSR mepc (0x341) <- 0x10000000').replace('CSR mtval (0x343) <- 0x00000000','CSR mtval (0x343) <- 0x10000000')
        trace = (frame(0,0,0x100000b7,'x1 <- 0x10000000') + frame(1,4,i(0,1,0,0,0x67))
                 + fault+'\n\n'+frame(3,256,i(0,0,0)))
        events = parse_sail_log(trace)
        self.assertEqual(len(events),3)
        self.assertEqual(events[1]['pc_after'],0x10000000)
        self.assertFalse(events[1].get('trap'))
        self.assertEqual(events[2]['instruction'],0)
        self.assertEqual(events[2]['trap_cause'],1)

    def test_rejects_missing_step_or_delimiter(self):
        for trace in (MEMORY.replace('[2]','[3]',1), MEMORY.replace('\n\n','\n',1)):
            with self.assertRaises(SailLogError): parse_sail_log(trace)

    def test_rejects_missing_effects_and_invalid_payload(self):
        for trace in (MEMORY.replace('mem[R,0x000000081] -> 0xFF\n',''),
                      MEMORY.replace('-> 0xFF','-> 0xFFFF'),
                      TRAP.replace('CSR mtval (0x343) <- 0x00000000\n',''),
                      TRAP.replace('mstatush (0x310) <- 0x00000000','mstatush (0x310) <- 0x00000001')):
            with self.assertRaises(SailLogError): parse_sail_log(trace)

    def test_rejects_unknown_or_contradictory_state_change(self):
        for trace in (CSR_TRACE.replace('-> 0x00000007','-> 0x00000008'),
                      MEMORY.replace('x1 <-','x1 =>'),
                      MEMORY.replace('x2 <- 0xFFFFFFFF','x2 <- 0xFFFFFFFF\nx3 <- 0xFFFFFFFF')):
            with self.assertRaises(SailLogError): parse_sail_log(trace)

    def test_comparison_detects_memory_trap_csr_and_control_corruption(self):
        for trace,index,field in [(MEMORY,1,'mem_read_data'),(MEMORY,2,'mem_write_mask'),
                                  (MEMORY,3,'pc_after'),(TRAP,0,'trap_cause'),(TRAP,0,'trap_value')]:
            expected = parse_sail_log(trace); actual = copy.deepcopy(expected)
            actual[index][field] ^= 1
            with self.assertRaises(ComparisonError): compare_traces(packets(expected),packets(actual))
        expected = parse_sail_log(CSR_TRACE); actual = copy.deepcopy(expected)
        actual[1]['csr_effects'][0]['new_value'] ^= 1
        with self.assertRaises(ComparisonError): compare_traces(packets(expected),packets(actual))


BASE = 0x6a5c0000


def spike(lines):
    """Spike -l --log-commits text: (pc offset, instruction, commit tail or exception tuple)."""
    out = []
    for pc, ins, effect in lines:
        out.append(f'core   0: 0x{BASE+pc:08x} (0x{ins:08x}) text')
        if isinstance(effect, tuple):
            out.append(f'core   0: exception {effect[0]}, epc 0x{BASE+pc:08x}')
            if effect[1] is not None: out.append(f'core   0:           tval 0x{effect[1]:08x}')
        else: out.append(f'core   0: 3 0x{BASE+pc:08x} (0x{ins:08x}){effect}')
    return '\n'.join(out)+'\n'


LUI, ADDI = constant(28,BASE+0x8000)
SPIKE = [(0,LUI,f' x28 0x{BASE+0x8000:08x}'),(4,ADDI,f' x28 0x{BASE+0x8000:08x}'),
         (8,store(28,0,1,0),f' mem 0x{BASE+0x8001:08x} 0x00'),(12,i(0,28,0,0,3),f' mem 0x{BASE+0x8000:08x}'),
         (16,i(2,28,0,0,3),f' x2  0xffffffff mem 0x{BASE+0x8000:08x}'),(20,csr(3,0,0xf12,2),' x3  0x00000005'),
         (24,0x13,''),(28,0x73,('trap_machine_ecall',None)),(256,0x30200073,' c768_mstatus 0x00001880 c784_mstatush 0x00000000'),
         (32,branch(0,0,0),''),(32,branch(0,0,0),'')]


class SpikeLogTest(unittest.TestCase):
    def convert(self, lines=SPIKE):
        return relocate(parse_sail_log(spike_sail_trace(spike(lines),{BASE+0x8000:0xff},0,{BASE+24:0x10500073},
                                                        {0xf12:(5,0)},0x1800)),BASE,0x10000)

    def test_relocation_shadow_loads_masks_and_traps(self):
        events = self.convert()
        self.assertEqual((events[0]['instruction'],events[1]['rd_value']),(constant(28,0x8000)[0],0x8000))
        self.assertEqual((events[3]['mem_address'],events[3]['mem_read_data']),(0x8000,0xff))
        self.assertEqual(events[4]['rd_value'],0xffffffff)
        self.assertEqual((events[5]['rd_value'],events[5]['csr_effects'][0]['old_value']),(0,0))
        self.assertEqual(events[6]['instruction'],0x10500073)
        self.assertEqual((events[7]['trap_cause'],events[7]['pc_after']),(11,256))
        self.assertEqual([e['new_value'] for e in events[7]['csr_effects']],[0x1800,28,11,0])
        self.assertEqual(events[8]['pc_after'],32)

    def test_rejects_inconsistent_spike_records(self):
        for lines in ([*SPIKE[:4],(16,i(2,28,0,0,3),f' x2  0x00000001 mem 0x{BASE+0x8000:08x}')],
                      [*SPIKE[:5],(20,csr(3,0,0xf12,2),' x3  0x00000006'),*SPIKE[6:]],
                      [*SPIKE[:7],(28,0x73,('trap_hypervisor_ecall',None))]):
            with self.assertRaises((SpikeLogError,SailLogError)): self.convert(lines)


if __name__ == '__main__':
    unittest.main()
