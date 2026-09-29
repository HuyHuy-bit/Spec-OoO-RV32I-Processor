`default_nettype none
module mmio_store_transaction (
  input wire clk_i, rst_i, kill_i, launch_i,
  output wire ready_o,
  input wire [12:0] id_i,
  input wire [31:0] address_i, write_data_i,
  input wire [3:0] byte_mask_i,
  input wire [1:0] size_i,
  input wire store_i, uncached_i, fault_i,
  input wire [31:0] cause_i, trap_value_i,
  input wire issue_allowed_i, head_valid_i,
  input wire [12:0] head_id_i,
  output wire authorize_o,
  output wire request_valid_o,
  input wire request_ready_i,
  output memory_protocol_pkg::mem_request_t request_o,
  input wire response_valid_i,
  output wire response_ready_o,
  input memory_protocol_pkg::mem_response_t response_i,
  output wire valid_o,
  input wire take_i,
  output wire [12:0] id_o, owner_id_o,
  output wire [31:0] address_o, write_data_o, cause_o, trap_value_o,
  output wire [3:0] byte_mask_o,
  output wire [1:0] size_o,
  output wire fault_o, busy_o, irrevocable_o,
  output logic fatal_o,
  output logic [2:0] fatal_reason_o
);
  import memory_protocol_pkg::*;
  typedef enum logic [2:0] {EMPTY, WAIT_HEAD, REQUEST, RESPONSE, RESULT} state_e;
  state_e state_q;
  logic [12:0] owner_q;
  logic [31:0] address_q, data_q, cause_q, trap_value_q;
  logic [3:0] mask_q, transaction_q;
  logic [1:0] size_q;
  logic fault_q, offered_q;
  logic [2:0] error_reason;
  wire active = !rst_i && !fatal_o;
  wire [3:0] expected_mask = (size_i == 0 ? 4'b0001 : size_i == 1 ? 4'b0011 : 4'b1111)
    << address_i[1:0];
  wire [31:0] data_mask = {{8{byte_mask_i[3]}}, {8{byte_mask_i[2]}},
    {8{byte_mask_i[1]}}, {8{byte_mask_i[0]}}};

  assign ready_o = active && !kill_i && !response_valid_i && state_q == EMPTY;
  assign busy_o = !rst_i && state_q != EMPTY;
  assign owner_id_o = busy_o ? owner_q : 13'd0;
  assign irrevocable_o = busy_o && offered_q;
  assign authorize_o = active && !kill_i && !response_valid_i && state_q == WAIT_HEAD
    && issue_allowed_i && head_valid_i && head_id_i == owner_q;
  assign request_valid_o = active && state_q == REQUEST;
  assign response_ready_o = active && state_q == RESPONSE;
  assign valid_o = active && !response_valid_i && state_q == RESULT && (!kill_i || irrevocable_o);
  assign id_o = valid_o ? owner_q : 13'd0;
  assign address_o = valid_o ? address_q : 32'd0;
  assign size_o = valid_o ? size_q : 2'd0;
  assign write_data_o = valid_o && !fault_q ? data_q : 32'd0;
  assign byte_mask_o = valid_o && !fault_q ? mask_q : 4'd0;
  assign fault_o = valid_o && fault_q;
  assign cause_o = valid_o ? cause_q : 32'd0;
  assign trap_value_o = valid_o ? trap_value_q : 32'd0;

  always_comb begin
    request_o = '0;
    if (request_valid_o) begin
      request_o.transaction_id = transaction_q;
      request_o.write = 1;
      request_o.uncached = 1;
      request_o.address = address_q;
      request_o.uncached_size = mem_uncached_size_e'(size_q);
      request_o.uncached_write_data = data_q;
      request_o.uncached_write_strobe = mask_q;
    end
    error_reason = 0;
    if (response_valid_i) begin
      if (state_q != RESPONSE) error_reason = 1;
      else if (response_i.transaction_id != transaction_q) error_reason = 2;
      else if (!(response_i.status inside {MEM_STATUS_OK, MEM_STATUS_ACCESS_FAULT})) error_reason = 3;
      else if (response_i.line_read_data != 0 || response_i.uncached_read_data != 0) error_reason = 4;
    end
  end

  // Authorization reserves the port; offered device writes remain owned through result acceptance.
  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      state_q <= EMPTY;
      transaction_q <= 0;
      offered_q <= 0;
      fatal_o <= 0;
      fatal_reason_o <= 0;
    end else if (!fatal_o) begin
      if (error_reason != 0) begin
        fatal_o <= 1;
        fatal_reason_o <= error_reason;
      end else case (state_q)
        EMPTY: if (launch_i && ready_o) begin
          owner_q <= id_i;
          address_q <= address_i;
          data_q <= write_data_i;
          mask_q <= byte_mask_i;
          size_q <= size_i;
          fault_q <= fault_i;
          cause_q <= fault_i ? cause_i : 32'd0;
          trap_value_q <= fault_i ? trap_value_i : 32'd0;
          offered_q <= 0;
          state_q <= fault_i ? RESULT : WAIT_HEAD;
        end
        WAIT_HEAD: if (kill_i) state_q <= EMPTY;
          else if (authorize_o) begin state_q <= REQUEST; offered_q <= 1; end
        REQUEST: if (request_ready_i) state_q <= RESPONSE;
        RESPONSE: if (response_valid_i) begin
          transaction_q <= transaction_q + 1'b1;
          fault_q <= response_i.status == MEM_STATUS_ACCESS_FAULT;
          cause_q <= response_i.status == MEM_STATUS_ACCESS_FAULT ? 32'd7 : 32'd0;
          trap_value_q <= response_i.status == MEM_STATUS_ACCESS_FAULT ? address_q : 32'd0;
          state_q <= RESULT;
        end
        RESULT: if (take_i || (kill_i && !offered_q)) state_q <= EMPTY;
        default: ;
      endcase
    end
  end
`ifndef SYNTHESIS
  always_ff @(posedge clk_i) if (!rst_i) begin
    assert (!launch_i || ready_o) else $fatal(1, "MMIO_STORE_CAPACITY");
    assert (!take_i || valid_o) else $fatal(1, "MMIO_STORE_TAKE");
    assert (!kill_i || !irrevocable_o) else $fatal(1, "MMIO_STORE_KILL");
    if (launch_i) begin
      assert (store_i && (fault_i || uncached_i)) else $fatal(1, "MMIO_STORE_CLASS");
      assert (size_i <= 2) else $fatal(1, "MMIO_STORE_SIZE");
      assert (fault_i || ((size_i != 1 || !address_i[0]) && (size_i != 2 || address_i[1:0] == 0)))
        else $fatal(1, "MMIO_STORE_ALIGNMENT");
      assert (fault_i ? byte_mask_i == 0 && write_data_i == 0 : byte_mask_i == expected_mask)
        else $fatal(1, "MMIO_STORE_MASK");
      assert ((write_data_i & ~data_mask) == 0) else $fatal(1, "MMIO_STORE_DATA");
      assert (!fault_i || ((cause_i == 6 || cause_i == 7) && trap_value_i == address_i))
        else $fatal(1, "MMIO_STORE_FAULT");
    end
  end
`endif
endmodule
`default_nettype wire
