`default_nettype none
module csr_single (
  input wire clk_i, rst_i, commit_i,
  input commit_event_pkg::commit_event_t commit_event_i,
  input single_lane_pkg::decoded_t decoded_i,
  input wire [31:0] source_i, pc_i,
  input wire trap_i,
  input wire [31:0] cause_i, trap_value_i,
  output logic legal_o,
  output logic [31:0] value_o,
  output wire [31:0] mtvec_o, mepc_o,
  output commit_event_pkg::commit_csr_effect_t [3:0] effects_o
);
  import platform_pkg::*;
  import single_lane_pkg::*;
  import commit_event_pkg::*;
  logic [31:0] csr_q [0:CSR_COUNT-1];
  logic write_query, found, cycle_write, instret_write;
  logic [31:0] operand, new_value;
  integer query_index;
  assign mtvec_o = csr_q[CSR_INDEX_MTVEC];
  assign mepc_o = csr_q[CSR_INDEX_MEPC];
  function automatic commit_csr_effect_t effect(input logic [11:0] address,
      input logic [31:0] new_data, mask, input csr_mask_reason_e reason);
    commit_csr_effect_t result;
    result = '0;
    for (int i = 0; i < CSR_COUNT; i++) if (CSR_ADDRESSES[i] == address) begin
      result.valid = 1;
      result.address = address;
      result.old_value = csr_q[i];
      result.new_value = new_data;
      result.read_mask = CSR_WRITE_MASKS[i] | CSR_FIXED_MASKS[i];
      result.write_mask = mask;
      result.mask_reason = reason;
    end
    return result;
  endfunction
  always_comb begin
    found = 0;
    query_index = 0;
    for (int i = 0; i < CSR_COUNT; i++) if (CSR_ADDRESSES[i] == decoded_i.csr) begin
      found = 1;
      query_index = i;
    end
    operand = decoded_i.funct3[2] ? {27'd0, decoded_i.zimm} : source_i;
    write_query = decoded_i.funct3[1:0] == 1 || decoded_i.zimm != 0;
    legal_o = found && !(write_query && CSR_READ_ONLY[query_index]);
    value_o = csr_q[query_index];
    case (decoded_i.funct3[1:0])
      1: new_value = operand;
      2: new_value = value_o | operand;
      3: new_value = value_o & ~operand;
      default: new_value = value_o;
    endcase
    new_value = ((new_value & CSR_WRITE_MASKS[query_index]) | (value_o & ~CSR_WRITE_MASKS[query_index]));
    new_value = (new_value & ~CSR_FIXED_MASKS[query_index]) | CSR_FIXED_VALUES[query_index];
  end
  always_comb begin
    effects_o = '0;
    if (trap_i) begin
      effects_o[0] = effect(CSR_MSTATUS,
        (csr_q[CSR_INDEX_MSTATUS] & ~32'h88) | {24'd0, csr_q[CSR_INDEX_MSTATUS][3], 7'd0}, CSR_MSTATUS_WRITE_MASK, CSR_MASK_TRAP_ENTRY);
      effects_o[1] = effect(CSR_MEPC, pc_i & 32'hfffffffc, CSR_MEPC_WRITE_MASK, CSR_MASK_TRAP_ENTRY);
      effects_o[2] = effect(CSR_MCAUSE, cause_i, CSR_MCAUSE_WRITE_MASK, CSR_MASK_TRAP_ENTRY);
      effects_o[3] = effect(CSR_MTVAL, trap_value_i, CSR_MTVAL_WRITE_MASK, CSR_MASK_TRAP_ENTRY);
    end else if (decoded_i.op == OP_MRET) begin
      effects_o[0] = effect(CSR_MSTATUS,
        (csr_q[CSR_INDEX_MSTATUS] & ~32'h88) | 32'h80 | {28'd0, csr_q[CSR_INDEX_MSTATUS][7], 3'd0}, CSR_MSTATUS_WRITE_MASK, CSR_MASK_MRET);
    end else if (decoded_i.op == OP_CSR && legal_o) begin
      effects_o[0] = effect(decoded_i.csr, write_query ? new_value : value_o,
                           write_query ? CSR_WRITE_MASKS[query_index] : 32'd0, CSR_MASK_INSTRUCTION);
    end
  end
  always_comb begin
    cycle_write = 0;
    instret_write = 0;
    for (int i = 0; i < 4; i++) if (commit_i && commit_event_i.csr_effects[i].valid
        && commit_event_i.csr_effects[i].write_mask != 0) begin
      if (commit_event_i.csr_effects[i].address inside {CSR_MCYCLE, CSR_MCYCLEH}) cycle_write = 1;
      if (commit_event_i.csr_effects[i].address inside {CSR_MINSTRET, CSR_MINSTRETH}) instret_write = 1;
    end
  end
  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      for (int i = 0; i < CSR_COUNT; i++) csr_q[i] <= CSR_RESETS[i];
    end else begin
      if (!csr_q[CSR_INDEX_MCOUNTINHIBIT][0] && !cycle_write)
        {csr_q[CSR_INDEX_MCYCLEH], csr_q[CSR_INDEX_MCYCLE]} <= {csr_q[CSR_INDEX_MCYCLEH], csr_q[CSR_INDEX_MCYCLE]} + 64'd1;
      if (commit_i && commit_event_i.retired && !csr_q[CSR_INDEX_MCOUNTINHIBIT][2] && !instret_write)
        {csr_q[CSR_INDEX_MINSTRETH], csr_q[CSR_INDEX_MINSTRET]} <= {csr_q[CSR_INDEX_MINSTRETH], csr_q[CSR_INDEX_MINSTRET]} + 64'd1;
      for (int e = 0; e < 4; e++) begin
        for (int i = 0; i < CSR_COUNT; i++) begin
          if (commit_i && commit_event_i.csr_effects[e].valid && commit_event_i.csr_effects[e].write_mask != 0
              && commit_event_i.csr_effects[e].address == CSR_ADDRESSES[i])
            csr_q[i] <= (csr_q[i] & ~commit_event_i.csr_effects[e].write_mask)
              | (commit_event_i.csr_effects[e].new_value & commit_event_i.csr_effects[e].write_mask);
        end
      end
    end
  end
endmodule
`default_nettype wire
