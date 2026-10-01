`default_nettype none
module decode_single (
  input wire [31:0] instruction_i,
  output single_lane_pkg::decoded_t decoded_o
);
  import single_lane_pkg::*;
  wire [6:0] opcode = instruction_i[6:0];
  wire [6:0] funct7 = instruction_i[31:25];
  wire [2:0] funct3 = instruction_i[14:12];
  always_comb begin
    decoded_o = '0;
    decoded_o.op = OP_ILLEGAL;
    decoded_o.funct3 = funct3;
    decoded_o.alternate = instruction_i[30];
    decoded_o.csr = instruction_i[31:20];
    decoded_o.zimm = instruction_i[19:15];
    case (opcode)
      7'h37, 7'h17: begin
        decoded_o.op = opcode == 7'h37 ? OP_LUI : OP_AUIPC;
        decoded_o.rd = instruction_i[11:7];
        decoded_o.immediate = {instruction_i[31:12], 12'd0};
      end
      7'h6f: begin
        decoded_o.op = OP_JAL;
        decoded_o.rd = instruction_i[11:7];
        decoded_o.immediate = {{11{instruction_i[31]}}, instruction_i[31], instruction_i[19:12],
                                instruction_i[20], instruction_i[30:21], 1'b0};
      end
      7'h67, 7'h03, 7'h13: begin
        decoded_o.rs1 = instruction_i[19:15];
        decoded_o.rd = instruction_i[11:7];
        decoded_o.immediate = {{20{instruction_i[31]}}, instruction_i[31:20]};
        if (opcode == 7'h67 && funct3 == 0) decoded_o.op = OP_JALR;
        if (opcode == 7'h03 && funct3 inside {3'd0, 3'd1, 3'd2, 3'd4, 3'd5}) decoded_o.op = OP_LOAD;
        if (opcode == 7'h13) begin
          decoded_o.immediate_alu = 1;
          decoded_o.op = OP_ALU;
          if ((funct3 == 1 && funct7 != 0) || (funct3 == 5 && !(funct7 inside {7'h00, 7'h20})))
            decoded_o.op = OP_ILLEGAL;
        end
      end
      7'h33: begin
        decoded_o.rs1 = instruction_i[19:15];
        decoded_o.rs2 = instruction_i[24:20];
        decoded_o.rd = instruction_i[11:7];
        if (funct7 == 0 || (funct7 == 7'h20 && funct3 inside {3'd0, 3'd5})) decoded_o.op = OP_ALU;
      end
      7'h63, 7'h23: begin
        decoded_o.rs1 = instruction_i[19:15];
        decoded_o.rs2 = instruction_i[24:20];
        if (opcode == 7'h63) begin
          decoded_o.immediate = {{19{instruction_i[31]}}, instruction_i[31], instruction_i[7],
                                  instruction_i[30:25], instruction_i[11:8], 1'b0};
          if (funct3 inside {3'd0, 3'd1, 3'd4, 3'd5, 3'd6, 3'd7}) decoded_o.op = OP_BRANCH;
        end else begin
          decoded_o.immediate = {{20{instruction_i[31]}}, instruction_i[31:25], instruction_i[11:7]};
          if (funct3 <= 2) decoded_o.op = OP_STORE;
        end
      end
      7'h0f: if (funct3 == 0) decoded_o.op = OP_FENCE;
        else if (funct3 == 1) decoded_o.op = OP_FENCE_I;
      7'h73: begin
        if (funct3 inside {3'd1, 3'd2, 3'd3, 3'd5, 3'd6, 3'd7}) begin
          decoded_o.op = OP_CSR;
          decoded_o.rd = instruction_i[11:7];
          if (!funct3[2]) decoded_o.rs1 = instruction_i[19:15];
        end else case (instruction_i)
          32'h00000073: decoded_o.op = OP_ECALL;
          32'h00100073: decoded_o.op = OP_EBREAK;
          32'h30200073: decoded_o.op = OP_MRET;
          32'h10500073: decoded_o.op = OP_WFI;
          default: decoded_o.op = OP_ILLEGAL;
        endcase
      end
      default: decoded_o.op = OP_ILLEGAL;
    endcase
    if (decoded_o.op == OP_ILLEGAL) begin
      decoded_o.rs1 = 0;
      decoded_o.rs2 = 0;
      decoded_o.rd = 0;
    end
  end
endmodule
`default_nettype wire
