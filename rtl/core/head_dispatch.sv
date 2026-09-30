`default_nettype none
module head_dispatch (
  input wire clk_i, rst_i, flush_i, drained_i,
  input wire [1:0] valid_i, serial_i,
  input wire cfi0_i, memory_i,
  input wire [31:0] instruction_i, pc_i,
  output wire [1:0] dispatch_valid_o, dispatch_solo_o,
  input wire [1:0] allocate_accept_i,
  input wire [12:0] allocate_id_i,
  input wire [5:0] source1_i, source2_i,
  output wire [1:0] queue_dispatch_o,
  input wire head_valid_i,
  input wire [12:0] head_id_i,
  input wire [31:0] head_pc_i,
  input wire recover_i,
  input wire [4:0] recover_slot_i,
  input wire retire_accept_i,
  input wire [12:0] retire_id_i,
  output wire busy_o, owner_valid_o, killed_o,
  output wire [12:0] owner_id_o,
  output wire [31:0] owner_instruction_o, owner_pc_o,
  output wire [11:0] owner_sources_o,
  output wire owner_memory_o
);
  logic valid_q;
  logic [12:0] id_q;
  logic [31:0] instruction_q, pc_q;
  logic [5:0] source_q, source2_q;
  logic memory_q;
  wire blocked = rst_i || flush_i || drained_i || recover_i;
  wire younger = 5'(id_q[4:0] - head_id_i[4:0]) > 5'(recover_slot_i - head_id_i[4:0]);
  wire kill = recover_i && younger;
  wire capture = allocate_accept_i[0] && serial_i[0];
  wire release_owner = retire_accept_i && retire_id_i == id_q;

  // A lane-one serialized operation is deferred; a held descriptor blocks younger dispatch.
  assign dispatch_valid_o[0] = valid_i[0] && !valid_q && !blocked;
  assign dispatch_valid_o[1] = dispatch_valid_o[0] && valid_i[1]
      && !cfi0_i && !serial_i[0] && !serial_i[1];
  assign dispatch_solo_o = dispatch_valid_o & serial_i;
  assign queue_dispatch_o = allocate_accept_i & ~serial_i & {2{!blocked && !valid_q}};
  assign busy_o = valid_q;
  assign killed_o = valid_q && (rst_i || flush_i || kill);
  assign owner_valid_o = valid_q && !blocked && head_valid_i
      && head_id_i == id_q && head_pc_i == pc_q;
  assign owner_id_o = valid_q ? id_q : 13'd0;
  assign owner_instruction_o = valid_q ? instruction_q : 32'd0;
  assign owner_pc_o = valid_q ? pc_q : 32'd0;
  assign owner_sources_o = valid_q ? {source2_q, source_q} : 12'd0;
  assign owner_memory_o = valid_q && memory_q;

  always_ff @(posedge clk_i) begin
    if (rst_i || flush_i || kill) valid_q <= 0;
    else if (release_owner) valid_q <= 0;
    else if (capture) begin
      valid_q <= 1;
      id_q <= allocate_id_i[12:0];
      instruction_q <= instruction_i[31:0];
      pc_q <= pc_i[31:0];
      source_q <= source1_i[5:0];
      source2_q <= source2_i;
      memory_q <= memory_i;
    end
  end

`ifndef SYNTHESIS
  wire load_op = instruction_i[6:0] == 7'h03
      && instruction_i[14:12] inside {3'd0, 3'd1, 3'd2, 3'd4, 3'd5};
  wire store_op = instruction_i[6:0] == 7'h23 && instruction_i[14:12] <= 2;
  wire system_op = instruction_i inside {32'h30200073, 32'h10500073}
      || (instruction_i[6:0] == 7'h73
      && instruction_i[14:12] inside {3'd1, 3'd2, 3'd3, 3'd5, 3'd6, 3'd7});
  always_ff @(posedge clk_i) if (!rst_i) begin
    assert (valid_i != 2'b10) else $fatal(1, "HEAD_DISPATCH_PREFIX");
    assert ((allocate_accept_i & ~dispatch_valid_o) == 0 && allocate_accept_i != 2'b10)
      else $fatal(1, "HEAD_DISPATCH_ALLOCATION");
    assert (!capture || (!cfi0_i && allocate_accept_i == 1
        && (memory_i ? (load_op || store_op) : system_op)))
      else $fatal(1, "HEAD_DISPATCH_OPERATION");
    assert (!capture || (!((!memory_i && (instruction_i == 32'h30200073 || instruction_i[14]))
        || instruction_i[19:15] == 0) || source1_i == 0))
      else $fatal(1, "HEAD_DISPATCH_SOURCE");
    assert (!capture || ((memory_i && store_op && instruction_i[24:20] != 0) || source2_i == 0))
      else $fatal(1, "HEAD_DISPATCH_SOURCE2");
    assert (!retire_accept_i || (owner_valid_o && retire_id_i == id_q))
      else $fatal(1, "HEAD_DISPATCH_RETIRE");
    assert (!drained_i || !valid_q) else $fatal(1, "HEAD_DISPATCH_DRAIN");
    assert (!recover_i || head_valid_i) else $fatal(1, "HEAD_DISPATCH_RECOVERY");
  end
`endif
endmodule
`default_nettype wire
