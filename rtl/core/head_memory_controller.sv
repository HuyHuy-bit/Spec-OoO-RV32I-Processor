`default_nettype none
module head_memory_controller (
  input wire clk_i, rst_i, recovery_i, cancel_i,
  input wire memory_valid_i,
  input wire [12:0] memory_id_i,
  input wire [31:0] instruction_i, pc_i,
  input wire head_valid_i,
  input wire [12:0] head_id_i,
  input wire [31:0] head_pc_i,
  input wire [1:0] source_ready_i,
  input wire [31:0] source1_i, source2_i,
  input wire issue_allowed_i,
  output wire read_request_o, prepare_o, busy_o,
  output wire admission_valid_o,
  output wire [12:0] admission_id_o,
  output wire [31:0] admission_address_o, admission_data_o,
  output wire [3:0] admission_mask_o,
  output wire [1:0] admission_size_o,
  input wire admit_valid_i,
  input wire [12:0] admit_id_i,
  output wire serial_offer_o,
  output wire [12:0] serial_id_o,
  output commit_event_pkg::commit_event_t serial_event_o,
  input wire serial_accept_i,
  output wire fault_offer_o,
  output wire [12:0] fault_id_o,
  output commit_event_pkg::commit_event_t fault_event_o,
  input wire fault_accept_i,
  output wire trap_ready_o,
  input wire trap_accept_i,
  output wire committed_o, irrevocable_o, fatal_o,
  output wire [3:0] fatal_sources_o,
  output wire [7:0] fatal_reason_o,
  output wire request_valid_o,
  input wire request_ready_i,
  output memory_protocol_pkg::mem_request_t request_o,
  input wire response_valid_i,
  output wire response_ready_o,
  input memory_protocol_pkg::mem_response_t response_i
);
  import single_lane_pkg::*;
  import commit_event_pkg::*;
  logic owner_q, launched_q, fault_sent_q;
  wire prepared, prepare_ready, desc_take, path_busy, result_valid, result_fault, result_store;
  wire commit_ready, commit_accept;
  wire [12:0] id, result_id;
  wire [31:0] instruction, pc, source1, source2, address, write_data, cause, trap_value;
  wire [31:0] result_address, result_data, result_write_data, result_cause, result_trap_value;
  wire [3:0] mask, result_mask;
  wire [1:0] size;
  wire store, load_unsigned, uncached, head_only, fault;
  wire [15:0] unused;
  wire active = !rst_i && !fatal_o;
  wire cancel = active && cancel_i && !irrevocable_o;
  wire owner_live = owner_q && head_valid_i && head_id_i == id && head_pc_i == pc;
  wire cached_store = prepared && store && !uncached && !fault && !launched_q;
  wire result_live = active && owner_live && result_valid && !cancel_i;
  wire accepted_serial = serial_offer_o && serial_accept_i;
  wire accepted_fault = fault_offer_o && fault_accept_i;
  wire accepted_trap = trap_ready_o && trap_accept_i;
  wire release_owner = accepted_serial || accepted_trap;
  wire result_take = accepted_trap || (accepted_serial && !cached_store);
  commit_event_t base_event;

  assign read_request_o = active && !owner_q && !path_busy && !recovery_i && !cancel_i
    && !response_valid_i && memory_valid_i && head_valid_i
    && memory_id_i == head_id_i && pc_i == head_pc_i;
  assign prepare_o = read_request_o && (&source_ready_i) && prepare_ready;
  assign busy_o = !rst_i && (owner_q || path_busy);
  assign admission_valid_o = active && owner_q && cached_store && !cancel_i;
  assign admission_id_o = admission_valid_o ? id : 13'd0;
  assign admission_address_o = admission_valid_o ? address : 32'd0;
  assign admission_data_o = admission_valid_o ? write_data : 32'd0;
  assign admission_mask_o = admission_valid_o ? mask : 4'd0;
  assign admission_size_o = admission_valid_o ? size : 2'd0;
  assign serial_offer_o = active && owner_live && !cancel_i
    && ((cached_store && commit_ready) || (result_valid && !result_fault));
  assign fault_offer_o = result_live && result_fault && !fault_sent_q;
  // Completion records the fault; only accepted trap entry releases its memory ownership.
  assign trap_ready_o = result_live && result_fault && fault_sent_q;
  assign serial_id_o = serial_offer_o ? id : 13'd0;
  assign fault_id_o = fault_offer_o ? id : 13'd0;

  always_comb begin
    base_event = '0;
    base_event.valid = 1;
    base_event.privilege = 3;
    base_event.instruction = instruction;
    base_event.pc_before = pc;
    base_event.pc_after = pc;
    base_event.rs1_addr = instruction[19:15];
    base_event.rs1_value = source1;
    base_event.rs2_addr = store ? instruction[24:20] : 5'd0;
    base_event.rs2_value = source2;
    serial_event_o = '0;
    fault_event_o = '0;
    if (serial_offer_o) begin
      serial_event_o = base_event;
      serial_event_o.retired = 1;
      serial_event_o.pc_after = pc + 32'd4;
      serial_event_o.mem_valid = 1;
      serial_event_o.mem_address = cached_store ? address : result_address;
      if (cached_store || result_store) begin
        serial_event_o.mem_write_mask = cached_store ? mask : result_mask;
        serial_event_o.mem_write_data = cached_store ? write_data : result_write_data;
      end else begin
        serial_event_o.rd_addr = instruction[11:7];
        serial_event_o.rd_value = instruction[11:7] == 0 ? 32'd0 : result_data;
        serial_event_o.rd_write_mask = instruction[11:7] == 0 ? 32'd0 : 32'hffffffff;
        serial_event_o.mem_read_mask = result_mask;
        serial_event_o.mem_read_data = (result_data << {result_address[1:0], 3'b000}) & byte_mask(result_mask);
      end
    end
    if (fault_offer_o) begin
      fault_event_o = base_event;
      fault_event_o.trap = 1;
      fault_event_o.trap_cause = result_cause;
      fault_event_o.trap_value = result_trap_value;
    end
  end

  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      owner_q <= 0;
      launched_q <= 0;
      fault_sent_q <= 0;
    end else if (active) begin
      if (desc_take) launched_q <= 1;
      if (accepted_fault) fault_sent_q <= 1;
      if (cancel || release_owner) begin
        owner_q <= 0;
        launched_q <= 0;
        fault_sent_q <= 0;
      end
      if (prepare_o) owner_q <= 1;
    end
  end

  // Keep the prepared instruction and operands until retirement, not just engine launch.
  load_store_prepare preparation (
    .clk_i, .rst_i, .flush_i(cancel), .launch_i(prepare_o), .ready_o(prepare_ready),
    .id_i(memory_id_i), .instruction_i, .pc_i, .source1_i, .source2_i,
    .valid_o(prepared), .take_i(release_owner), .id_o(id), .instruction_o(instruction), .pc_o(pc),
    .source1_o(source1), .source2_o(source2), .address_o(address), .write_data_o(write_data),
    .cause_o(cause), .trap_value_o(trap_value), .byte_mask_o(mask), .size_o(size),
    .store_o(store), .load_unsigned_o(load_unsigned), .uncached_o(uncached), .head_only_o(head_only), .fault_o(fault)
  );
  memory_transaction_path transactions (
    .clk_i, .rst_i, .recovery_i, .kill_i(cancel),
    .desc_valid_i(owner_live && prepared && !launched_q), .desc_take_o(desc_take),
    .id_i(id), .address_i(address), .write_data_i(write_data), .byte_mask_i(mask), .size_i(size),
    .store_i(store), .unsigned_i(load_unsigned), .uncached_i(uncached), .head_only_i(head_only),
    .fault_i(fault), .cause_i(cause), .trap_value_i(trap_value),
    .head_valid_i(owner_live), .issue_allowed_i(issue_allowed_i && owner_live),
    .admit_valid_i, .head_id_i, .admit_id_i, .commit_ready_o(commit_ready),
    .commit_i(accepted_serial && cached_store), .commit_accept_o(commit_accept),
    .valid_o(result_valid), .take_i(result_take), .id_o(result_id), .address_o(result_address),
    .data_o(result_data), .write_data_o(result_write_data), .cause_o(result_cause),
    .trap_value_o(result_trap_value), .byte_mask_o(result_mask), .size_o(unused[1:0]),
    .store_o(result_store), .fault_o(result_fault), .busy_o(path_busy),
    .owner_valid_o(unused[2]), .owner_id_o(unused[15:3]), .committed_o, .irrevocable_o,
    .fatal_o, .fatal_sources_o, .fatal_reason_o,
    .request_valid_o, .request_ready_i, .request_o, .response_valid_i, .response_ready_o, .response_i
  );
`ifndef SYNTHESIS
  logic released_q;
  logic [12:0] released_id_q;
  logic [31:0] released_pc_q;
  always_ff @(posedge clk_i) begin
    if (rst_i) released_q <= 0;
    else begin
      released_q <= release_owner;
      if (release_owner) begin released_id_q <= id; released_pc_q <= pc; end
      assert (!cancel_i || !irrevocable_o) else $fatal(1, "HEAD_MEMORY_MMIO_CANCEL");
      assert (!cancel_i || !(serial_accept_i || fault_accept_i || trap_accept_i))
        else $fatal(1, "HEAD_MEMORY_CANCEL_ACCEPT");
      assert ((!serial_accept_i || serial_offer_o) && (!fault_accept_i || fault_offer_o)
        && (!trap_accept_i || trap_ready_o)) else $fatal(1, "HEAD_MEMORY_ACCEPT");
      assert (commit_accept == (accepted_serial && cached_store)) else $fatal(1, "HEAD_MEMORY_COMMIT");
      assert (!result_valid || (owner_q && launched_q && result_id == id)) else $fatal(1, "HEAD_MEMORY_RESULT");
      assert (!prepare_o || (!owner_q && !path_busy)) else $fatal(1, "HEAD_MEMORY_PREPARE");
      if (active && !cancel_i) begin
        assert (!admit_valid_i || admission_valid_o) else $fatal(1, "HEAD_MEMORY_ADMISSION");
        assert (!released_q || !(memory_valid_i && memory_id_i == released_id_q && pc_i == released_pc_q))
          else $fatal(1, "HEAD_MEMORY_RELEASE");
      end
    end
  end
`endif
endmodule
`default_nettype wire
