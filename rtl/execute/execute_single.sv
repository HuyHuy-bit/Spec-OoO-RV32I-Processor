`default_nettype none
module execute_single (
  input single_lane_pkg::decoded_t decoded_i,
  input wire [31:0] pc_i, instruction_i, source1_i, source2_i, csr_value_i, mepc_i,
  input wire csr_legal_i,
  output logic [31:0] result_o, next_pc_o, address_o,
  output logic trap_o, taken_o,
  output logic [31:0] cause_o, trap_value_o
);
  import single_lane_pkg::*;
  import platform_pkg::*;
  logic [31:0] operand2;
  always_comb begin
    result_o = 0;
    next_pc_o = pc_i + 4;
    address_o = source1_i + decoded_i.immediate;
    trap_o = 0;
    cause_o = 0;
    trap_value_o = 0;
    taken_o = 0;
    operand2 = decoded_i.immediate_alu ? decoded_i.immediate : source2_i;
    case (decoded_i.op)
      OP_ALU: case (decoded_i.funct3)
        0: result_o = !decoded_i.immediate_alu && decoded_i.alternate ? source1_i - operand2 : source1_i + operand2;
        1: result_o = source1_i << operand2[4:0];
        2: result_o = {31'd0, $signed(source1_i) < $signed(operand2)};
        3: result_o = {31'd0, source1_i < operand2};
        4: result_o = source1_i ^ operand2;
        5: result_o = decoded_i.alternate ? 32'($signed(source1_i) >>> operand2[4:0]) : source1_i >> operand2[4:0];
        6: result_o = source1_i | operand2;
        7: result_o = source1_i & operand2;
      endcase
      OP_LUI: result_o = decoded_i.immediate;
      OP_AUIPC: result_o = pc_i + decoded_i.immediate;
      OP_JAL, OP_JALR: begin
        result_o = pc_i + 4;
        taken_o = 1;
        next_pc_o = decoded_i.op == OP_JAL ? pc_i + decoded_i.immediate : address_o & 32'hfffffffe;
      end
      OP_BRANCH: begin
        case (decoded_i.funct3)
          0: taken_o = source1_i == source2_i;
          1: taken_o = source1_i != source2_i;
          4: taken_o = $signed(source1_i) < $signed(source2_i);
          5: taken_o = $signed(source1_i) >= $signed(source2_i);
          6: taken_o = source1_i < source2_i;
          7: taken_o = source1_i >= source2_i;
          default: taken_o = 0;
        endcase
        if (taken_o) next_pc_o = pc_i + decoded_i.immediate;
      end
      OP_LOAD, OP_STORE: begin
        if ((decoded_i.funct3[1:0] == 1 && address_o[0]) || (decoded_i.funct3[1:0] == 2 && address_o[1:0] != 0)) begin
          trap_o = 1;
          cause_o = decoded_i.op == OP_STORE ? CAUSE_STORE_ADDRESS_MISALIGNED : CAUSE_LOAD_ADDRESS_MISALIGNED;
        end else if (!accessible(address_o, decoded_i.op == OP_STORE)) begin
          trap_o = 1;
          cause_o = decoded_i.op == OP_STORE ? CAUSE_STORE_ACCESS_FAULT : CAUSE_LOAD_ACCESS_FAULT;
        end
        if (trap_o) trap_value_o = address_o;
      end
      OP_CSR: begin
        result_o = csr_value_i;
        if (!csr_legal_i) begin
          trap_o = 1;
          cause_o = CAUSE_ILLEGAL_INSTRUCTION;
          trap_value_o = instruction_i;
        end
      end
      OP_MRET: next_pc_o = mepc_i;
      OP_ECALL, OP_EBREAK, OP_ILLEGAL: begin
        trap_o = 1;
        if (decoded_i.op == OP_ECALL) cause_o = CAUSE_ENVIRONMENT_CALL_FROM_M_MODE;
        else if (decoded_i.op == OP_EBREAK) begin
          cause_o = CAUSE_BREAKPOINT;
          trap_value_o = pc_i;
        end else begin
          cause_o = CAUSE_ILLEGAL_INSTRUCTION;
          trap_value_o = instruction_i;
        end
      end
      default: begin end
    endcase
    if (next_pc_o[1:0] != 0) begin
      trap_o = 1;
      cause_o = CAUSE_INSTRUCTION_ADDRESS_MISALIGNED;
      trap_value_o = next_pc_o;
    end
  end
endmodule
`default_nettype wire
